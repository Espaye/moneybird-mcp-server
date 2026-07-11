"""Entrypoint for the Moneybird MCP server.

The implementation now lives in the ``moneybird`` package (split by concern:
``config`` → ``client`` → ``formatting`` → ``safety`` → ``sync`` → ``invoicing``
→ ``tools``). This module stays as the thing you run:

    python moneybird_mcp_server.py

It also re-exports the package's public helpers so existing imports such as
``import moneybird_mcp_server as server`` keep working unchanged.
"""
from __future__ import annotations

import logging
import os
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

logger = logging.getLogger("moneybird_mcp")

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
    import uvicorn
    from starlette.middleware import Middleware

    # Bind to loopback by default. The cloudflared tunnel runs on the same host
    # and connects to localhost, so this does not break tunnelling — it just
    # stops the server from listening on every network interface. Set
    # MCP_HOST=0.0.0.0 explicitly only if you genuinely need external binding
    # (and then you really want MCP_AUTH_TOKEN set too).
    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", "8000"))
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    # "http" = streamable HTTP (current MCP transport, endpoint /mcp);
    # "sse" = legacy SSE (endpoint /sse), kept as default for existing deployments.
    transport = os.environ.get("MCP_TRANSPORT", "sse").strip().lower()
    if transport not in {"sse", "http"}:
        logger.error("MCP_TRANSPORT must be 'sse' or 'http', not %r.", transport)
        raise SystemExit(1)

    middleware = []
    if auth_token:
        middleware.append(Middleware(SharedSecretAuthMiddleware, token=auth_token))
        logger.info("Shared-secret auth ENABLED on the MCP endpoint.")
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set: the MCP endpoint has NO authentication. "
            "This is only safe because host=%s. Set MCP_AUTH_TOKEN before exposing "
            "the server beyond loopback.",
            host,
        )
        if host not in {"127.0.0.1", "localhost", "::1"}:
            logger.error(
                "Refusing to start: host=%s is non-loopback but MCP_AUTH_TOKEN is unset. "
                "Set MCP_AUTH_TOKEN to allow non-loopback binding.",
                host,
            )
            raise SystemExit(1)

    app = mcp.http_app(transport=transport, middleware=middleware or None)
    endpoint = "/sse" if transport == "sse" else "/mcp"
    logger.info(
        "Starting Moneybird MCP server on %s:%s (%s at %s)",
        host,
        port,
        "legacy SSE" if transport == "sse" else "streamable HTTP",
        endpoint,
    )
    uvicorn.run(app, host=host, port=port)
