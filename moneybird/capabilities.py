"""Server-side capability policy independent of tool discovery and model prompts."""
from __future__ import annotations

import os
from enum import StrEnum

from .config import MoneybirdError
from .credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
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
    raise MoneybirdError(
        "Moneybird writes are disabled by server policy"
        f"{suffix}. Set {CAPABILITY_MODE_ENV}=write_enabled only for an explicitly "
        "supervised deployment whose confirmation and reconciliation limits you accept."
    )
