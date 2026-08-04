"""Shared machinery for the guarded two-step write flow.

Every write pair follows the same discipline:

* ``prepare_*`` validates, builds a preview, and stages the action via
  :func:`stage_write` — nothing is sent to Moneybird.
* ``*_from_approval`` runs :func:`run_approved_write`, which atomically claims
  the stored approval, enforces the duplicate-suppression fingerprint, executes,
  persists the outcome, and exports an audit event in one place.

An executor receives ``(client, payload)`` and returns the tool result dict. Three
reserved keys let it steer the envelope: ``_status`` (the ``status`` field of the
response, default ``"done"``), ``_audit`` (extra fields recorded in the audit
log entry), and the required ``_audit_result``. There is deliberately no default
success: every executor must explicitly return ``"success"``,
``"partial_failure"``, ``"verification_failed"``, ``"ambiguous"``, or
``"failed_pre_write"``.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Callable

from ..capabilities import capability_mode, require_write_capability, writes_enabled
from ..config import MoneybirdError
from ..formatting import duplicate_fingerprint, iso_now
from ..safety import (
    approval_execution_state,
    classify_failed_write,
    make_approval,
    pop_approval,
    record_approval_outcome,
    record_approval_phase,
)
from . import _context as ctx

EXECUTOR_AUDIT_RESULTS = {
    "success",
    "failed_pre_write",
    "partial_failure",
    "verification_failed",
    "ambiguous",
}

_ACTIVE_EXECUTION: ContextVar[tuple[str, str] | None] = ContextVar(
    "moneybird_active_write_execution",
    default=None,
)


def mark_write_dispatch_started() -> None:
    """Persist the last safe retry boundary immediately before an API mutation."""

    active = _ACTIVE_EXECUTION.get()
    if active is None:
        raise MoneybirdError(
            "Write dispatch marker used outside a claimed approval execution."
        )
    approval_id, administration_id = active
    record_approval_phase(
        approval_id,
        "dispatching",
        administration_id=administration_id,
    )


def mark_write_verifying() -> None:
    """Persist that the upstream call returned and independent checks have begun."""

    active = _ACTIVE_EXECUTION.get()
    if active is None:
        raise MoneybirdError(
            "Write verification marker used outside a claimed approval execution."
        )
    approval_id, administration_id = active
    record_approval_phase(
        approval_id,
        "verifying",
        administration_id=administration_id,
    )


def stage_write(
    action: str,
    *,
    summary: str,
    payload: dict[str, Any],
    preview: dict[str, Any],
    fingerprint: str = "",
) -> dict[str, Any]:
    """Store a pending write and return the approval envelope shown to the user."""
    # Every enabled write gets a semantic fingerprint. Callers can provide a
    # state-aware fingerprint; the canonical payload is the conservative
    # fallback for simpler actions.
    fingerprint = fingerprint or duplicate_fingerprint(action, payload)
    payload = {**payload, "fingerprint": fingerprint}
    approval = make_approval(action, payload, summary)
    approval["payload"] = payload
    approval["preview"] = preview
    approval["capability_mode"] = capability_mode().value
    approval["execution_available"] = writes_enabled()
    if not approval["execution_available"]:
        approval["warning"] = (
            "This preview is not executed. The server is in read_only capability "
            "mode, so execution will be rejected unless an operator explicitly "
            "restarts it with MONEYBIRD_CAPABILITY_MODE=write_enabled."
        )
    return approval


def run_approved_write(
    client: Any,
    approval_id: str,
    action: str,
    executor: Callable[[Any, dict[str, Any]], dict[str, Any]],
) -> dict[str, Any]:
    """Claim the approval, guard against duplicates, execute, and persist the outcome."""
    # Enforce deployment policy before claiming the single-use token. A rejected
    # read-only attempt must leave the approval available for an authorised run.
    require_write_capability(action=action)
    pending = pop_approval(
        approval_id, action, administration_id=client.administration_id
    )
    payload = pending["payload"]
    fingerprint = payload.get("fingerprint", "")
    if fingerprint and ctx.audit_log_contains_success(action, fingerprint):
        record_approval_outcome(
            approval_id,
            "duplicate_suppressed",
            administration_id=client.administration_id,
        )
        raise MoneybirdError(
            f"This exact {action} action already completed successfully according "
            "to the local audit log."
        )
    execution_token = _ACTIVE_EXECUTION.set(
        (approval_id, str(client.administration_id))
    )
    try:
        result = executor(client, payload)
    except Exception as exc:
        # Never leak a claimed execution into later calls in the same async
        # context, even if durable-state inspection itself encounters an error.
        _ACTIVE_EXECUTION.reset(execution_token)
        execution = approval_execution_state(
            approval_id,
            administration_id=client.administration_id,
        )
        audit_result = classify_failed_write(exc, phase=execution["phase"])
        record_approval_outcome(
            approval_id,
            audit_result,
            administration_id=client.administration_id,
            error=str(exc),
        )
        ctx.append_failed_audit_log(
            action,
            fingerprint=fingerprint,
            error=str(exc),
            result=audit_result,
        )
        if audit_result == "ambiguous":
            raise MoneybirdError(
                f"{exc} The write result is ambiguous: it may already have been "
                "applied in Moneybird. Verify the administration before retrying; "
                "this approval cannot be executed again."
            ) from exc
        raise
    else:
        _ACTIVE_EXECUTION.reset(execution_token)
    if not isinstance(result, dict):
        message = f"Write executor for {action} returned no structured outcome."
        record_approval_outcome(
            approval_id,
            "verification_failed",
            administration_id=client.administration_id,
            error=message,
        )
        ctx.append_failed_audit_log(
            action,
            fingerprint=fingerprint,
            error=message,
            result="verification_failed",
        )
        raise MoneybirdError(message)

    audit_extra = result.pop("_audit", {})
    audit_result = result.pop("_audit_result", None)
    if audit_result not in EXECUTOR_AUDIT_RESULTS:
        message = (
            f"Write executor for {action} must explicitly return _audit_result as "
            f"one of: {', '.join(sorted(EXECUTOR_AUDIT_RESULTS))}."
        )
        record_approval_outcome(
            approval_id,
            "verification_failed",
            administration_id=client.administration_id,
            error=message,
        )
        ctx.append_failed_audit_log(
            action,
            fingerprint=fingerprint,
            error=message,
            partial={"executor_result": result},
            result="verification_failed",
        )
        raise MoneybirdError(message)
    execution = approval_execution_state(
        approval_id,
        administration_id=client.administration_id,
    )
    if (
        execution["phase"] in {"dispatching", "verifying"}
        and audit_result == "failed_pre_write"
    ):
        message = (
            f"Write executor for {action} reported failed_pre_write after its "
            "durable dispatch boundary. The result requires reconciliation."
        )
        record_approval_outcome(
            approval_id,
            "ambiguous",
            administration_id=client.administration_id,
            error=message,
        )
        ctx.append_failed_audit_log(
            action,
            fingerprint=fingerprint,
            error=message,
            partial={"executor_result": result},
            result="ambiguous",
        )
        raise MoneybirdError(message)
    preflight_failure = (
        execution["phase"] == "preflight"
        and audit_result == "failed_pre_write"
    )
    if execution["phase"] != "verifying" and not preflight_failure:
        message = (
            f"Write executor for {action} returned without recording both the "
            "dispatch and verification phases."
        )
        record_approval_outcome(
            approval_id,
            "verification_failed",
            administration_id=client.administration_id,
            error=message,
        )
        ctx.append_failed_audit_log(
            action,
            fingerprint=fingerprint,
            error=message,
            partial={"executor_result": result},
            result="verification_failed",
        )
        raise MoneybirdError(message)
    record_approval_outcome(
        approval_id,
        audit_result,
        administration_id=client.administration_id,
    )
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
