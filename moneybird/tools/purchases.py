"""Purchase-side document tools: reads, plus reconcile-against-supplier-pattern writes."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from ..config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from ..formatting import (
    compact_document_summary,
    compact_general_journal_summary,
    duplicate_fingerprint,
    money_decimal,
)
from ..invoicing import details_attributes_payload
from ..purchase_reconcile import (
    build_reconcile_purchase_invoice,
    scan_purchase_invoices_for_attention,
)
from ._params import ApprovalId, FilterString, Limit, Page, Period
from ._registry import mcp
from ._writes import run_approved_write, stage_write
from . import _context as ctx


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_purchase_invoices(
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird purchase invoices."""
    client = ctx.get_client()
    documents = client.list_documents(
        "purchase_invoice",
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "purchase_invoices": [
            compact_document_summary("purchase_invoice", item, client.administration_id)
            for item in documents
        ],
        "page": page,
        "count": len(documents),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_receipts(
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird receipts and cash/other-account expense documents."""
    client = ctx.get_client()
    documents = client.list_documents(
        "receipt",
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "receipts": [
            compact_document_summary("receipt", item, client.administration_id)
            for item in documents
        ],
        "page": page,
        "count": len(documents),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_general_journal_documents(
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird general journal documents."""
    client = ctx.get_client()
    documents = client.list_documents(
        "general_journal_document",
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "general_journal_documents": [
            compact_general_journal_summary(item, client.administration_id)
            for item in documents
        ],
        "page": page,
        "count": len(documents),
    }


PurchaseTemplateKind = Annotated[
    str,
    Field(description="Document kind to reconcile: 'purchase_invoice' (default) or 'receipt'."),
]


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def review_purchase_invoices(
    period: Period = "",
    limit: Limit = 100,
    contact_id: str = "",
    kind: PurchaseTemplateKind = "purchase_invoice",
) -> dict[str, Any]:
    """Use this to find purchase invoices that need attention: still in 'new' state, or booked
    differently than the same supplier usually books (fewer lines, missing ledger accounts, or a
    flipped incl/excl-tax flag). Each flagged invoice suggests a canonical prior invoice to use as
    the reconcile reference. Read-only; it never changes anything."""
    client = ctx.get_client()
    return scan_purchase_invoices_for_attention(
        client,
        kind=kind,
        period=period,
        limit=limit,
        contact_id=contact_id,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_reconcile_purchase_invoice(
    document_id: Annotated[str, Field(description="Id of the purchase invoice or receipt to fix.")],
    reference_document_id: Annotated[
        str,
        Field(description="Id of a known-good invoice from the same supplier to copy the line structure from. Leave empty to auto-pick that supplier's most fully-split recent invoice."),
    ] = "",
    kind: PurchaseTemplateKind = "purchase_invoice",
    target_total: Annotated[
        str,
        Field(description="Override the target total (incl tax) as a decimal string. Leave empty to keep the invoice's own current total, so the document total is preserved to the cent."),
    ] = "",
    relabel_period: Annotated[
        bool,
        Field(description="Replace the reference month label in each copied line description with the target invoice's month (e.g. 'juni 2026' -> 'juli 2026')."),
    ] = True,
) -> dict[str, Any]:
    """Prepare a fix that reproduces a supplier's established booking on a botched invoice.

    Copies the reference invoice's lines (descriptions, ledgers, tax rates) onto the target and
    scales the prices to the target total, so the document total stays fixed to the cent. Existing
    target lines are reused by ledger+tax to keep their identity; extra lines are added and leftover
    lines removed. When totals differ the per-line split is scaled proportionally — an assumption
    flagged in the preview. Nothing is written; confirm the preview, then call
    reconcile_purchase_invoice_from_approval.
    """
    client = ctx.get_client()
    built = build_reconcile_purchase_invoice(
        client,
        document_id=document_id,
        document_kind=kind,
        reference_document_id=reference_document_id,
        target_total=target_total,
        relabel_period=relabel_period,
    )
    payload = built["payload"]
    preview = built["preview"]
    fingerprint = duplicate_fingerprint("reconcile_purchase_invoice", payload)
    summary = (
        f"Reconcile {payload['document_kind']} {payload['document_id']} to "
        f"{preview['line_count_after']} line(s), total {preview['total_after']} "
        f"(reference {preview['reference_document_id']})"
    )
    return stage_write(
        "reconcile_purchase_invoice",
        summary=summary,
        payload=payload,
        preview=preview,
        fingerprint=fingerprint,
    )


def _execute_reconcile(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload["document_kind"]
    document_id = payload["document_id"]
    expected_total = money_decimal(payload["expected_total_incl_tax"])

    client.update_document(
        kind,
        document_id,
        {
            "prices_are_incl_tax": payload["prices_are_incl_tax"],
            "details_attributes": details_attributes_payload(payload["details_attributes"]),
        },
    )

    after = client.get_document(kind, document_id)
    total_after = money_decimal(after.get("total_price_incl_tax"))
    verified = abs(total_after - expected_total) < Decimal("0.005")
    lines = [
        {
            "id": str(detail.get("id")),
            "description": detail.get("description"),
            "price": f'{money_decimal(detail.get("price")):.2f}',
            "ledger_account_id": str(detail.get("ledger_account_id") or ""),
            "tax_rate_id": str(detail.get("tax_rate_id") or ""),
        }
        for detail in (after.get("details") or [])
    ]
    return {
        "_status": "completed" if verified else "completed_with_verification_errors",
        "_audit": {
            "document_id": document_id,
            "total_after": f"{total_after:.2f}",
            "verified_total_unchanged": verified,
        },
        "document_id": document_id,
        "document_kind": kind,
        "reference": after.get("reference"),
        "state": after.get("state"),
        "prices_are_incl_tax": after.get("prices_are_incl_tax"),
        "total_expected": f"{expected_total:.2f}",
        "total_after": f"{total_after:.2f}",
        "verified_total_unchanged": verified,
        "lines": lines,
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def reconcile_purchase_invoice_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed a prepared purchase-invoice
    reconcile. It applies the new line structure, re-fetches the document, and verifies the total
    matches the expected amount to the cent."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "reconcile_purchase_invoice", _execute_reconcile
    )


