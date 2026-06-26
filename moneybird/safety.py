"""Write-safety machinery: in-memory approval tokens (TTL) and the JSONL audit log."""
from __future__ import annotations

import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .config import (
    APPROVAL_TTL_MINUTES,
    MoneybirdError,
)
from .formatting import iso_now

AUDIT_LOG_PATH = Path(".moneybird_audit_log.jsonl")
PENDING_APPROVALS: dict[str, dict[str, Any]] = {}



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




def append_audit_log(entry: dict[str, Any]) -> None:
    log_entry = {"timestamp": iso_now(), **entry}
    with AUDIT_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(log_entry, ensure_ascii=True) + "\n")




def audit_log_contains_success(action: str, fingerprint: str) -> bool:
    if not AUDIT_LOG_PATH.exists():
        return False
    for raw_line in AUDIT_LOG_PATH.read_text(encoding="utf-8").splitlines():
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
) -> None:
    append_audit_log(
        {
            "action": action,
            "fingerprint": fingerprint,
            "result": "failed",
            "error": error,
            "partial": partial or {},
        }
    )
