"""Entrypoint for the Moneybird MCP server.

The implementation now lives in the ``moneybird`` package (split by concern:
``config`` → ``client`` → ``formatting`` → ``safety`` → ``sync`` → ``invoicing``
→ ``tools``). This module stays as the thing you run:

    python moneybird_mcp_server.py

It also re-exports the package's public helpers so existing imports such as
``import moneybird_mcp_server as server`` keep working unchanged.
"""
from __future__ import annotations

from decimal import Decimal

from moneybird.auth import SharedSecretAuthMiddleware
from moneybird.config import MoneybirdError
from moneybird.formatting import (
    build_filter_string,
    contact_invoice_email,
    duplicate_fingerprint,
    money_decimal,
    normalize_document_kind,
    render_contact_delivery_table,
    render_preview_table,
    year_period_for_date,
)
from moneybird.invoicing import (
    apply_batch_group_merge_checks,
    build_preview_row,
    classify_sales_invoice_send,
    compare_merge_snapshots,
    evaluate_merge_compatibility,
    recurring_sales_invoice_delivery_issue,
    validate_document_ledger_target,
    validate_general_journal_entries,
)
from moneybird.safety import (
    AUDIT_LOG_PATH,
    append_audit_log,
    audit_log_contains_success,
)
from moneybird.sync import ensure_sync_index_shape
from moneybird.tools import mcp

# Names re-exported purely for backward compatibility (tests and ad-hoc imports).
__all__ = [
    "mcp",
    "Decimal",
    "MoneybirdError",
    "SharedSecretAuthMiddleware",
    "build_filter_string",
    "contact_invoice_email",
    "duplicate_fingerprint",
    "money_decimal",
    "normalize_document_kind",
    "render_contact_delivery_table",
    "render_preview_table",
    "year_period_for_date",
    "apply_batch_group_merge_checks",
    "build_preview_row",
    "classify_sales_invoice_send",
    "compare_merge_snapshots",
    "evaluate_merge_compatibility",
    "recurring_sales_invoice_delivery_issue",
    "validate_document_ledger_target",
    "validate_general_journal_entries",
    "AUDIT_LOG_PATH",
    "append_audit_log",
    "audit_log_contains_success",
    "ensure_sync_index_shape",
]


if __name__ == "__main__":
    from moneybird.server import main

    # Historical default: legacy SSE over HTTP, so existing deployments that run
    # `python moneybird_mcp_server.py` keep working. The `moneybird-mcp` console
    # script (moneybird/server.py) defaults to stdio for local MCP clients.
    main(default_transport="sse")
