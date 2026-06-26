"""Write-safety machinery: in-memory approval tokens (TTL) and the JSONL audit log."""
from __future__ import annotations

import json
import re
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import (
    APPROVAL_TTL_MINUTES,
    MoneybirdError,
)
from .credentials import get_active_administration_id
from .formatting import iso_now

# The audit log is per administration so tenants never share a write history.
AUDIT_LOG_BASENAME = ".moneybird_audit_log"
LEGACY_AUDIT_LOG_PATH = Path(f"{AUDIT_LOG_BASENAME}.jsonl")
# Backward-compatible alias for the legacy single-file path (re-exported by the entrypoint).
AUDIT_LOG_PATH = LEGACY_AUDIT_LOG_PATH
PENDING_APPROVALS: dict[str, dict[str, Any]] = {}


def audit_log_path(administration_id: str | None = None) -> Path:
    administration_id = administration_id or get_active_administration_id()
    if not administration_id:
        return LEGACY_AUDIT_LOG_PATH
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", str(administration_id))
    return Path(f"{AUDIT_LOG_BASENAME}_{safe}.jsonl")



def make_approval(action: str, payload: dict[str, Any], summary: str) -> dict[str, Any]:
    approval_id = secrets.token_urlsafe(18)
    expires_at = datetime.now(UTC) + timedelta(minutes=APPROVAL_TTL_MINUTES)
    PENDING_APPROVALS[approval_id] = {
        "action": action,
        "payload": payload,
        "summary": summary,
        "expires_at": expires_at,
        "created_at": datetime.now(UTC),
    }
    return {
        "approval_id": approval_id,
        "action": action,
        "summary": summary,
        "expires_at": expires_at.isoformat(),
        "warning": (
            "This action is not executed yet. Ask the user for explicit confirmation "
            "before calling the matching *_from_approval tool."
        ),
    }




def pop_approval(approval_id: str, expected_action: str) -> dict[str, Any]:
    pending = PENDING_APPROVALS.get(approval_id)
    if not pending:
        raise MoneybirdError(
            "Unknown approval_id. Prepare the action again before executing it."
        )

    if pending["action"] != expected_action:
        raise MoneybirdError(
            f"approval_id is for {pending['action']}, not {expected_action}."
        )

    if datetime.now(UTC) > pending["expires_at"]:
        PENDING_APPROVALS.pop(approval_id, None)
        raise MoneybirdError("approval_id expired. Prepare the action again.")

    return PENDING_APPROVALS.pop(approval_id)




def append_audit_log(entry: dict[str, Any], administration_id: str | None = None) -> None:
    administration_id = administration_id or get_active_administration_id()
    log_entry = {"timestamp": iso_now(), **entry, "administration_id": administration_id}
    path = audit_log_path(administration_id)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_entry, ensure_ascii=True) + "\n")




def audit_log_contains_success(
    action: str,
    fingerprint: str,
    administration_id: str | None = None,
) -> bool:
    administration_id = administration_id or get_active_administration_id()
    # Check this tenant's log first, then the pre-multitenant shared log for back-compat.
    candidate_paths = [audit_log_path(administration_id)]
    if LEGACY_AUDIT_LOG_PATH not in candidate_paths:
        candidate_paths.append(LEGACY_AUDIT_LOG_PATH)
    for path in candidate_paths:
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
