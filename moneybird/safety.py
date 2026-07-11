"""Write-safety machinery: durable approval tokens (TTL) and the JSONL audit log.

Approvals are persisted in a small SQLite database inside :func:`~moneybird.config.data_dir`
so a prepared write survives a server restart and works when the server runs with more
than one worker process (prepare and execute may land on different processes). The audit
log stays JSONL: append-only, greppable, one file per administration.
"""
from __future__ import annotations

import json
import re
import secrets
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import (
    APPROVAL_TTL_MINUTES,
    MoneybirdError,
    data_dir,
)
from .credentials import get_active_administration_id
from .formatting import iso_now

# The audit log is per administration so tenants never share a write history.
AUDIT_LOG_BASENAME = ".moneybird_audit_log"
LEGACY_AUDIT_LOG_PATH = Path(f"{AUDIT_LOG_BASENAME}.jsonl")
# Backward-compatible alias for the legacy single-file path (re-exported by the entrypoint).
AUDIT_LOG_PATH = LEGACY_AUDIT_LOG_PATH

APPROVALS_DB_BASENAME = "moneybird_approvals.sqlite3"


def audit_log_path(administration_id: str | None = None) -> Path:
    administration_id = administration_id or get_active_administration_id()
    if not administration_id:
        return data_dir() / LEGACY_AUDIT_LOG_PATH.name
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(administration_id))
    return data_dir() / f"{AUDIT_LOG_BASENAME}_{safe}.jsonl"


def approvals_db_path() -> Path:
    return data_dir() / APPROVALS_DB_BASENAME


def _approvals_connection() -> sqlite3.Connection:
    connection = sqlite3.connect(approvals_db_path())
    connection.execute(
        """
        CREATE TABLE IF NOT EXISTS approvals (
            approval_id TEXT PRIMARY KEY,
            action TEXT NOT NULL,
            payload TEXT NOT NULL,
            summary TEXT NOT NULL,
            administration_id TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
        """
    )
    return connection


def _purge_expired(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DELETE FROM approvals WHERE expires_at < ?",
        (datetime.now(UTC).isoformat(),),
    )


def clear_pending_approvals() -> None:
    """Remove every staged approval (used by tests and manual resets)."""
    with _approvals_connection() as connection:
        connection.execute("DELETE FROM approvals")


def pending_approval_count() -> int:
    with _approvals_connection() as connection:
        _purge_expired(connection)
        (count,) = connection.execute("SELECT COUNT(*) FROM approvals").fetchone()
    return int(count)


def make_approval(action: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
    approval_id = secrets.token_urlsafe(18)
    now = datetime.now(UTC)
    expires_at = now + timedelta(minutes=APPROVAL_TTL_MINUTES)
    administration_id = get_active_administration_id()
    if not administration_id:
        raise MoneybirdError(
            "Cannot prepare a write without an active Moneybird administration."
        )
    with _approvals_connection() as connection:
        _purge_expired(connection)
        connection.execute(
            "INSERT INTO approvals VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                approval_id,
                action,
                # default=str keeps rare non-JSON scalars (Decimal, dates) storable;
                # write payloads must be JSON-shaped anyway to be sendable to Moneybird.
                json.dumps(payload, ensure_ascii=True, default=str),
                summary,
                str(administration_id),
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
    return {
        "approval_id": approval_id,
        "action": action,
        "summary": summary,
        "administration_id": administration_id,
        "expires_at": expires_at.isoformat(),
        "warning": (
            "This action is not executed yet. Ask the user for explicit confirmation "
            "before calling the matching *_from_approval tool."
        ),
    }


def pop_approval(
    approval_id: str,
    expected_action: str,
    *,
    administration_id: str | None = None,
) -> dict[str, Any]:
    with _approvals_connection() as connection:
        row = connection.execute(
            "SELECT action, payload, summary, administration_id, expires_at "
            "FROM approvals WHERE approval_id = ?",
            (approval_id,),
        ).fetchone()
        if not row:
            raise MoneybirdError(
                "Unknown approval_id. Prepare the action again before executing it."
            )
        action, payload_json, summary, prepared_administration_id, expires_at = row

        if action != expected_action:
            raise MoneybirdError(
                f"approval_id is for {action}, not {expected_action}."
            )

        if (
            prepared_administration_id
            and str(prepared_administration_id) != str(administration_id or "")
        ):
            raise MoneybirdError(
                "approval_id belongs to a different Moneybird administration. "
                "Prepare the action again for the active administration."
            )

        if datetime.now(UTC) > datetime.fromisoformat(expires_at):
            connection.execute(
                "DELETE FROM approvals WHERE approval_id = ?", (approval_id,)
            )
            raise MoneybirdError("approval_id expired. Prepare the action again.")

        connection.execute(
            "DELETE FROM approvals WHERE approval_id = ?", (approval_id,)
        )

    return {
        "action": action,
        "payload": json.loads(payload_json),
        "summary": summary,
        "administration_id": prepared_administration_id,
        "expires_at": expires_at,
    }


def append_audit_log(entry: dict[str, Any], administration_id: str | None = None) -> None:
    administration_id = administration_id or get_active_administration_id()
    log_entry = {"timestamp": iso_now(), **entry, "administration_id": administration_id}
    path = audit_log_path(administration_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_entry, ensure_ascii=True) + "\n")


def _audit_log_candidates(administration_id: str | None) -> list[Path]:
    """Current path plus pre-data-dir/pre-multitenant locations, for back-compat reads."""
    candidates = [audit_log_path(administration_id)]
    if administration_id:
        safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(administration_id))
        candidates.append(Path(f"{AUDIT_LOG_BASENAME}_{safe}.jsonl"))
    candidates.append(LEGACY_AUDIT_LOG_PATH)
    unique: list[Path] = []
    for path in candidates:
        if path.resolve() not in {existing.resolve() for existing in unique}:
            unique.append(path)
    return unique


def audit_log_contains_success(
    action: str,
    fingerprint: str,
    administration_id: str | None = None,
) -> bool:
    administration_id = administration_id or get_active_administration_id()
    for path in _audit_log_candidates(administration_id):
        if not path.exists():
            continue
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            entry = json.loads(raw_line)
            if (
                entry.get("action") == action
                and entry.get("fingerprint") == fingerprint
                and entry.get("result") == "success"
            ):
                return True
    return False


def append_failed_audit_log(
    action: str,
    *,
    fingerprint: str = "",
    error: str,
    partial: dict[str, Any] | None = None,
    administration_id: str | None = None,
) -> None:
    append_audit_log(
        {
            "action": action,
            "fingerprint": fingerprint,
            "result": "failed",
            "error": error,
            "partial": partial or {},
        },
        administration_id=administration_id,
    )
