"""Server-side capability policy independent of tool discovery and model prompts."""
from __future__ import annotations

import os
from enum import StrEnum

from .config import MoneybirdError
from .credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    get_active_administration_id,
    get_credential_mode,
)

CAPABILITY_MODE_ENV = "MONEYBIRD_CAPABILITY_MODE"


class CapabilityMode(StrEnum):
    READ_ONLY = "read_only"
    WRITE_ENABLED = "write_enabled"


def capability_mode() -> CapabilityMode:
    raw = os.environ.get(CAPABILITY_MODE_ENV, CapabilityMode.READ_ONLY.value)
    normalized = raw.strip().lower().replace("-", "_")
    try:
        return CapabilityMode(normalized)
    except ValueError as exc:
        choices = ", ".join(mode.value for mode in CapabilityMode)
        raise MoneybirdError(
            f"{CAPABILITY_MODE_ENV} must be one of: {choices}; got {raw!r}."
        ) from exc


def writes_enabled() -> bool:
    return capability_mode() is CapabilityMode.WRITE_ENABLED


def require_write_capability(*, action: str | None = None) -> None:
    if get_credential_mode() == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        raise MoneybirdError(
            "Moneybird writes are disabled in hosted_request_only mode because "
            "approvals are not yet bound to an independently authenticated "
            "principal, session, and grant."
        )
    if writes_enabled():
        return
    suffix = f" for action {action!r}" if action else ""
    message = (
        "Moneybird writes are disabled by server policy"
        f"{suffix}. Set {CAPABILITY_MODE_ENV}=write_enabled only for an explicitly "
        "supervised deployment whose confirmation and reconciliation limits you accept."
    )
    administration_id = get_active_administration_id()
    if administration_id:
        # Import lazily so the policy module remains usable while safety state is
        # being initialized. A denied execution is still an audit event even
        # though the pending approval deliberately remains reusable.
        from .safety import append_audit_log

        append_audit_log(
            {
                "action": action or "write",
                "result": "policy_blocked",
                "error": message,
            },
            administration_id=administration_id,
        )
    raise MoneybirdError(message)
