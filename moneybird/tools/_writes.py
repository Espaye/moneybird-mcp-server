"""Shared machinery for the guarded two-step write flow.

Every write pair follows the same discipline:

* ``prepare_*`` validates, builds a preview, and stages the action via
  :func:`stage_write` — nothing is sent to Moneybird.
* ``*_from_approval`` runs :func:`run_approved_write`, which pops the stored
  approval, enforces the duplicate-suppression fingerprint, executes, and writes
  the audit log (success or failure) in one place.

An executor receives ``(client, payload)`` and returns the tool result dict. Two
reserved keys let it steer the envelope: ``_status`` (the ``status`` field of the
response, default ``"done"``), ``_audit`` (extra fields recorded in the audit
log entry), and ``_audit_result`` (default ``"success"``; use
``"partial_failure"`` when a non-transactional batch returns verified partial
progress instead of raising).
"""
from __future__ import annotations

from typing import Any, Callable

from ..config import MoneybirdError
from ..formatting import iso_now
from ..safety import make_approval, pop_approval
from . import _context as ctx


def stage_write(
    action: str,
    *,
    summary: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    fingerprint: str = "",
) -> dict[str, Any]:
    """Store a pending write and return the approval envelope shown to the user."""
    if fingerprint:
        payload = {**payload, "fingerprint": fingerprint}
    approval = make_approval(action, payload, summary)
    approval["payload"] = payload
    approval["preview"] = preview
    return approval


def run_approved_write(
    client: Any,
    approval_id: str,
    action: str,
    executor: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Pop the approval, guard against duplicates, execute, and audit-log the outcome."""
    pending = pop_approval(
        approval_id, action, administration_id=client.administration_id
    )
    payload = pending["payload"]
    fingerprint = payload.get("fingerprint", "")
    if fingerprint and ctx.audit_log_contains_success(action, fingerprint):
        raise MoneybirdError(
            f"This exact {action} action already completed successfully according "
            "to the local audit log."
        )
    try:
        result = executor(client, payload)
    except Exception as exc:
        ctx.append_failed_audit_log(action, fingerprint=fingerprint, error=str(exc))
        raise
    audit_extra = result.pop("_audit", {})
    audit_result = result.pop("_audit_result", "success")
    ctx.append_audit_log(
        {
            "action": action,
            "fingerprint": fingerprint,
            "result": audit_result,
            **audit_extra,
        }
    )
    status = result.pop("_status", "done")
    response = {
        "status": status,
        "approved_at": iso_now(),
        "summary": pending["summary"],
        **result,
    }
    if fingerprint:
        response["fingerprint"] = fingerprint
    return response
