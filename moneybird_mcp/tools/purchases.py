"""Purchase-side document tools: reads, plus reconcile-against-supplier-pattern writes."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from ..config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    MoneybirdError,
)
from ..credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    get_credential_mode,
)
from ..document_lines import line_signatures
from ..formatting import (
    compact_document_summary,
    compact_general_journal_summary,
    document_kind_config,
    duplicate_fingerprint,
    money_decimal,
    normalize_document_kind,
)
from ..invoicing import details_attributes_payload
from ..purchase_reconcile import (
    build_explicit_purchase_invoice_reconcile,
    build_reconcile_purchase_invoice,
)
from ..purchase_review import scan_purchase_invoices_for_attention
from . import _context as ctx
from ._params import (
    ApprovalId,
    DocumentListKind,
    FilterString,
    Limit,
    MoneybirdId,
    Page,
    Period,
)
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_purchase_documents(
    kind: DocumentListKind = "purchase_invoice",
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """List inkoopfacturen (purchase invoices, the invoices your suppliers/leveranciers send
    you), bonnen/bonnetjes (receipts, cash-account expense documents), or
    memoriaalboekingen (general journal documents). Pick one with `kind`.

    A supplier has no sales invoices; its documents live here. To find unpaid ones, filter
    on state — note that an unpaid purchase invoice is 'late' or 'new', never 'open'
    (filter='state:late|new')."""
    normalized = normalize_document_kind(kind)
    client = ctx.get_client()
    documents = client.list_documents(
        normalized,
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    summarize = (
        (lambda item: compact_general_journal_summary(item, client.administration_id))
        if normalized == "general_journal_document"
        else (
            lambda item: compact_document_summary(
                normalized, item, client.administration_id
            )
        )
    )
    collection = document_kind_config(normalized)["collection_name"]
    return {
        collection: [summarize(item) for item in documents],
        "kind": normalized,
        "page": page,
        "count": len(documents),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_purchase_invoice_by_reference(
    reference: Annotated[
        str,
        Field(
            description=(
                "Exact supplier invoice number/reference, for example '2112179204'. "
                "Use this instead of broad search when the user names an inkoopfactuur."
            )
        ),
    ],
) -> dict[str, Any]:
    """Find one purchase invoice directly by its exact supplier reference.

    This uses Moneybird's server-side document filter and returns the current line,
    attachment, payment, and version data needed for a safe processing preview.
    """
    client = ctx.get_client()
    document = client.get_document_by_reference("purchase_invoice", reference)
    ledger_accounts = {
        str(account.get("id")): account for account in client.list_ledger_accounts()
    }
    tax_rates = {str(rate.get("id")): rate for rate in client.list_tax_rates()}
    summary = compact_document_summary(
        "purchase_invoice",
        document,
        client.administration_id,
    )
    return {
        "purchase_invoice": {
            **summary,
            "version": document.get("version"),
            "updated_at": document.get("updated_at"),
            "prices_are_incl_tax": document.get("prices_are_incl_tax"),
            "details": [
                _purchase_detail_with_account_names(
                    detail,
                    ledger_accounts=ledger_accounts,
                    tax_rates=tax_rates,
                )
                for detail in (document.get("details") or [])
            ],
            "attachments": [
                {
                    "id": str(attachment.get("id") or ""),
                    "filename": attachment.get("filename"),
                    "content_type": attachment.get("content_type"),
                    "size": attachment.get("size"),
                }
                for attachment in (document.get("attachments") or [])
            ],
            "payments": document.get("payments") or [],
        }
    }


def _purchase_detail_with_account_names(
    detail: dict[str, Any],
    *,
    ledger_accounts: dict[str, dict[str, Any]],
    tax_rates: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    ledger_id = str(detail.get("ledger_account_id") or "")
    tax_id = str(detail.get("tax_rate_id") or "")
    ledger = ledger_accounts.get(ledger_id) or {}
    tax = tax_rates.get(tax_id) or {}
    return {
        "id": str(detail.get("id") or ""),
        "description": detail.get("description"),
        "amount": detail.get("amount"),
        "price": detail.get("price"),
        "ledger_account_id": ledger_id,
        "ledger_account_name": ledger.get("name"),
        "tax_rate_id": tax_id,
        "tax_rate_name": tax.get("name"),
        "tax_percentage": tax.get("percentage"),
    }


PurchaseTemplateKind = Annotated[
    str,
    Field(description="Document kind to reconcile: 'purchase_invoice' (default) or 'receipt'."),
]

AttachmentDocumentKind = Annotated[
    str,
    Field(
        description=(
            "Document kind the attachment belongs to: 'purchase_invoice' (default), "
            "'receipt', or 'general_journal_document'."
        )
    ),
]


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def read_document_attachment(
    document_id: Annotated[
        MoneybirdId, Field(description="Id of the document whose attachment to read.")
    ],
    attachment_id: Annotated[
        str,
        Field(
            pattern=r"^(?:|[0-9]+)$",
            description=(
                "Id of the attachment (from the document's 'attachments' array). Leave "
                "empty when the document has exactly one attachment; with several, the "
                "tool returns the list so you can pick."
            )
        ),
    ] = "",
    kind: AttachmentDocumentKind = "purchase_invoice",
) -> dict[str, Any]:
    """Use this to read the actual (PDF) attachment behind a purchase invoice or receipt —
    for example to get the real per-line amounts of a supplier invoice instead of assuming
    them from a prior month. Downloads into bounded memory, does not retain the file, and
    returns the PDF's untrusted text layer when one exists. It never changes Moneybird.
    Extracted amounts feed prepare_reconcile_purchase_invoice through desired_lines, never
    a direct write."""
    from ..attachments import DEFAULT_MAX_ATTACHMENT_BYTES, extract_pdf_text

    if get_credential_mode() == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        raise MoneybirdError(
            "Attachment parsing is disabled in hosted_request_only mode until "
            "durable capacity, backpressure, abuse, and lifecycle controls exist."
        )
    client = ctx.get_client()
    document = client.get_document(kind, document_id)
    attachments = document.get("attachments") or []
    listing = [
        {
            "id": str(item.get("id")),
            "filename": item.get("filename"),
            "content_type": item.get("content_type"),
            "size": item.get("size"),
        }
        for item in attachments
    ]
    if not attachments:
        return {
            "document_id": str(document.get("id")),
            "document_kind": kind,
            "attachments": [],
            "note": "This document has no attachments.",
        }
    if not attachment_id and len(attachments) > 1:
        return {
            "document_id": str(document.get("id")),
            "document_kind": kind,
            "attachments": listing,
            "note": "Multiple attachments; call again with the attachment_id you want.",
        }
    wanted = str(attachment_id) if attachment_id else listing[0]["id"]
    selected = next((item for item in listing if item["id"] == wanted), None)
    if selected is None:
        return {
            "document_id": str(document.get("id")),
            "document_kind": kind,
            "attachments": listing,
            "note": f"Attachment {wanted} not found on this document; pick one of the listed ids.",
        }

    declared_size = selected.get("size")
    if declared_size not in {None, ""}:
        try:
            parsed_size = int(declared_size)
        except (TypeError, ValueError) as exc:
            raise MoneybirdError("Attachment metadata contains an invalid size.") from exc
        if parsed_size < 0 or parsed_size > DEFAULT_MAX_ATTACHMENT_BYTES:
            raise MoneybirdError(
                f"Attachment exceeds the {DEFAULT_MAX_ATTACHMENT_BYTES}-byte limit."
            )

    data, content_type = client.download_attachment(kind, document_id, wanted)

    return {
        "document_id": str(document.get("id")),
        "document_kind": kind,
        "reference": document.get("reference"),
        "attachment": selected,
        "content_type": content_type,
        "size_bytes": len(data),
        "retention": "none",
        "text": extract_pdf_text(data, content_type=content_type),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def review_purchase_invoices(
    period: Period = "",
    limit: Limit = 100,
    contact_id: str = "",
    kind: PurchaseTemplateKind = "purchase_invoice",
    include_description_mapping_checks: Annotated[
        bool,
        Field(
            description=(
                "Include advisory text-similarity checks for familiar descriptions "
                "booked to a different ledger or tax rate. Disable to run only "
                "deterministic state and supplier-pattern checks."
            )
        ),
    ] = True,
) -> dict[str, Any]:
    """Use this to find purchase invoices that need attention: still in 'new' state, booked
    differently than the same supplier usually books, or carrying a familiar description on a
    different ledger/tax destination. Contact-specific scans use complete versioned history.
    Each flagged invoice suggests a canonical prior invoice to use as the reconcile reference.
    Read-only; it never changes anything."""
    client = ctx.get_client()
    return scan_purchase_invoices_for_attention(
        client,
        kind=kind,
        period=period,
        limit=limit,
        contact_id=contact_id,
        include_description_mapping_checks=include_description_mapping_checks,
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
    desired_lines: Annotated[
        list[dict[str, Any]] | None,
        Field(
            description=(
                "Optional exact line allocation transcribed from the actual invoice/PDF. "
                "Each dict requires description, price, ledger_account_id, and tax_rate_id; "
                "amount must be 1 when supplied. When set, no proportional reference scaling "
                "is used and the calculated total must equal the current invoice total."
            )
        ),
    ] = None,
    prices_are_incl_tax: Annotated[
        bool | None,
        Field(
            description=(
                "Line-price mode for desired_lines. Leave empty to preserve the current flag; "
                "set explicitly when the PDF amounts are inclusive or exclusive of tax."
            )
        ),
    ] = None,
    source_note: Annotated[
        str,
        Field(
            description=(
                "Short provenance shown in the preview for desired_lines, for example "
                "'PDF attachment, page 2'."
            )
        ),
    ] = "",
) -> dict[str, Any]:
    """Prepare a fix that reproduces a supplier's established booking on a botched invoice.

    With ``desired_lines``, uses exact PDF-derived amounts and refuses any allocation that changes
    the current total. Without ``desired_lines``, copies the reference invoice's descriptions,
    ledgers, and tax rates and scales its prices to the target total. Existing lines are reused by
    ledger+tax to keep their identity; extra lines are added and leftover lines removed. A document
    version snapshot is stored in both modes so execution aborts if the invoice changes after the
    preview. Nothing is written until reconcile_purchase_invoice_from_approval is called.
    """
    client = ctx.get_client()
    if desired_lines is not None:
        if reference_document_id or target_total:
            raise MoneybirdError(
                "When desired_lines is supplied, leave reference_document_id and target_total empty."
            )
        built = build_explicit_purchase_invoice_reconcile(
            client,
            document_id=document_id,
            document_kind=kind,
            desired_lines=desired_lines,
            prices_are_incl_tax=prices_are_incl_tax,
            source_note=source_note,
        )
    else:
        if prices_are_incl_tax is not None or source_note:
            raise MoneybirdError(
                "prices_are_incl_tax and source_note apply only when desired_lines is supplied."
            )
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
    if preview.get("mode") == "explicit_lines":
        summary = (
            f"Reconcile {payload['document_kind']} {payload['document_id']} from exact "
            f"source amounts to {preview['line_count_after']} line(s), total "
            f"{preview['total_after']}"
        )
    else:
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


def _validate_reconcile_preflight(
    before: dict[str, Any],
    payload: dict[str, Any],
) -> str:
    document_id = payload["document_id"]
    expected_version = str(payload.get("expected_version") or "")
    current_version = str(before.get("version") or "")
    expected_updated_at = str(payload.get("expected_updated_at") or "")
    current_updated_at = str(before.get("updated_at") or "")
    if expected_version and current_version != expected_version:
        raise MoneybirdError(
            f"Document {document_id} changed after the preview (version "
            f"{expected_version} -> {current_version}). Prepare the reconciliation again."
        )
    if (
        not expected_version
        and expected_updated_at
        and current_updated_at != expected_updated_at
    ):
        raise MoneybirdError(
            f"Document {document_id} changed after the preview (updated_at "
            f"{expected_updated_at} -> {current_updated_at}). Prepare the reconciliation again."
        )
    expected_total_before = money_decimal(
        payload.get(
            "expected_total_before",
            payload["expected_total_incl_tax"],
        )
    )
    current_total = money_decimal(before.get("total_price_incl_tax"))
    if abs(current_total - expected_total_before) >= Decimal("0.005"):
        raise MoneybirdError(
            f"Document {document_id} total changed after the preview: expected "
            f"{expected_total_before:.2f}, now {current_total:.2f}. "
            "Prepare the reconciliation again."
        )
    return current_version


def _execute_reconcile(client: Any, payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload["document_kind"]
    document_id = payload["document_id"]
    expected_total = money_decimal(payload["expected_total_incl_tax"])
    before = client.get_document(kind, document_id)
    if str(before.get("id") or "") != str(document_id):
        raise MoneybirdError(
            f"Document {document_id} lookup returned a different record. Prepare again."
        )
    current_version = _validate_reconcile_preflight(before, payload)

    mark_write_dispatch_started()
    client.update_document(
        kind,
        document_id,
        {
            "prices_are_incl_tax": payload["prices_are_incl_tax"],
            "details_attributes": details_attributes_payload(payload["details_attributes"]),
        },
    )

    mark_write_verifying()
    after = client.get_document(kind, document_id)
    record_id_matches = str(after.get("id") or "") == str(document_id)
    total_after = money_decimal(after.get("total_price_incl_tax"))
    verified_total = abs(total_after - expected_total) < Decimal("0.005")
    expected_lines = payload.get("expected_lines") or []
    verified_lines = not expected_lines or line_signatures(
        after.get("details") or []
    ) == line_signatures(expected_lines)
    verified_tax_mode = bool(after.get("prices_are_incl_tax")) == bool(
        payload["prices_are_incl_tax"]
    )
    verified = (
        record_id_matches
        and verified_total
        and verified_lines
        and verified_tax_mode
    )
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
        "_audit_result": "success" if verified else "verification_failed",
        "_audit": {
            "document_id": document_id,
            "total_after": f"{total_after:.2f}",
            "version_before": current_version or None,
            "version_after": after.get("version"),
            "verified_total_unchanged": verified_total,
            "record_id_matches": record_id_matches,
            "verified_lines_match": verified_lines,
            "verified_prices_are_incl_tax": verified_tax_mode,
        },
        "document_id": document_id,
        "document_kind": kind,
        "reference": after.get("reference"),
        "state": after.get("state"),
        "prices_are_incl_tax": after.get("prices_are_incl_tax"),
        "total_expected": f"{expected_total:.2f}",
        "total_after": f"{total_after:.2f}",
        "version_before": current_version or None,
        "version_after": after.get("version"),
        "verified_total_unchanged": verified_total,
        "record_id_matches": record_id_matches,
        "verified_lines_match": verified_lines,
        "verified_prices_are_incl_tax": verified_tax_mode,
        "lines": lines,
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def reconcile_purchase_invoice_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed a prepared purchase-invoice
    reconcile. It applies the new line structure, re-fetches the document, and verifies the total
    matches the expected amount to the cent."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "reconcile_purchase_invoice", _execute_reconcile
    )


