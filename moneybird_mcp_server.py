"""Entrypoint for the Moneybird MCP server.

The implementation now lives in the ``moneybird_mcp`` package (split by concern:
``config`` → ``client`` → ``formatting`` → ``safety`` → ``sync`` → ``invoicing``
→ ``tools``). This module stays as the thing you run:

    python moneybird_mcp_server.py

It also re-exports the package's public helpers so existing imports such as
``import moneybird_mcp_server as server`` keep working unchanged.
"""
from __future__ import annotations

if __name__ == "__main__":
    # Parse an explicit --env-file before importing the registered tool surface
    # or any compatibility helpers that may consume server configuration.
    from moneybird_mcp.server import main

    main(default_transport="sse")
else:
    from decimal import Decimal

    from moneybird_mcp.auth import SharedSecretAuthMiddleware
    from moneybird_mcp.config import MoneybirdError
    from moneybird_mcp.formatting import (
        build_filter_string,
        contact_invoice_email,
        duplicate_fingerprint,
        money_decimal,
        normalize_document_kind,
        render_contact_delivery_table,
        render_preview_table,
        year_period_for_date,
    )
    from moneybird_mcp.invoicing import (
        apply_batch_group_merge_checks,
        build_preview_row,
        classify_sales_invoice_send,
        compare_merge_snapshots,
        evaluate_merge_compatibility,
        recurring_sales_invoice_delivery_issue,
        validate_document_ledger_target,
        validate_general_journal_entries,
    )
    from moneybird_mcp.safety import (
        AUDIT_LOG_PATH,
        append_audit_log,
        audit_log_contains_success,
    )
    from moneybird_mcp.sync import ensure_sync_index_shape
    from moneybird_mcp.tools import mcp

    # Names re-exported purely for backward compatibility.
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
