"""Single patch point for effectful dependencies of the tool modules.

Tool modules call these through the module object (``ctx.get_client()``), so tests
can redirect every tool at once by patching ``moneybird_mcp.tools._context``.
"""
from __future__ import annotations

from ..client import get_client
from ..safety import (
    append_audit_log,
    append_failed_audit_log,
    audit_log_contains_success,
)

__all__ = [
    "get_client",
    "append_audit_log",
    "append_failed_audit_log",
    "audit_log_contains_success",
]
