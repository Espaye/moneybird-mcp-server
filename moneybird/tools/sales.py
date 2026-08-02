"""Sales-side reads and guarded single-invoice writes (draft, send, pause/resume, credit)."""
from __future__ import annotations

from typing import Annotated, Any

from pydantic import Field

from ..config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
    MoneybirdError,
)
from ..formatting import (
    api_url,
    build_filter_string,
    clean_dict,
    contact_title,
    document_contact_title,
    duplicate_fingerprint,
    invoice_title,
    money_decimal,
)
from ..invoicing import (
    build_merge_snapshot_from_invoice,
    build_recent_sales_invoice_send_method_audit,
    evaluate_merge_compatibility,
    infer_contact_invoice_defaults,
    list_scheduled_merge_candidates,
    resolve_contact_reference,
)
from ..write_contracts import (
    SALES_INVOICE_LINE_DECIMALS,
    SALES_INVOICE_LINE_FIELDS,
    compare_controlled_rows,
    verify_sales_invoice_payload,
)
from . import _context as ctx
from ._params import (
    ApprovalId,
    ContactId,
    FilterString,
    Limit,
    OptionalDateString,
    Page,
    Period,
    SalesInvoiceId,
)
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)


def _invoice_precondition_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        key: record.get(key)
        for key in ("version", "updated_at", "state", "paused", "invoice_date")
        if record.get(key) is not None
    }


def _changed_invoice_preconditions(
    record: dict[str, Any],
    expected: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    return {
        key: {"expected": value, "actual": record.get(key)}
        for key, value in expected.items()
        if str(record.get(key) or "") != str(value or "")
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_sales_invoices(
    limit: Limit = 10,
    page: Page = 1,
    state: Annotated[str, Field(description="Invoice state: 'all', 'draft', 'open', 'scheduled', 'late', 'reminded', 'paid', or 'uncollectible'.")] = "all",
    reference: Annotated[str, Field(description="Filter on the invoice reference text.")] = "",
    contact_id: Annotated[str, Field(description="Only invoices for this contact id.")] = "",
    period: Period = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird sales invoices filtered by state, reference, contact, or period."""
    client = ctx.get_client()
    invoices = client.list_sales_invoices(
        limit=limit,
        page=page,
        state=state,
        reference=reference,
        contact_id=contact_id,
        period=period,
    )
    result = {
        "sales_invoices": [
            {
                "id": str(item.get("id")),
                "title": invoice_title(item),
                "invoice_id": item.get("invoice_id"),
                "state": item.get("state"),
                "reference": item.get("reference"),
                "invoice_date": item.get("invoice_date"),
                "total_price_incl_tax": item.get("total_price_incl_tax"),
                "contact_id": item.get("contact_id"),
                "url": api_url("sales_invoices", str(item.get("id")), client.administration_id),
            }
            for item in invoices
        ],
        "page": page,
        "count": len(invoices),
    }
    if contact_id and not invoices:
        # A supplier invoices us, so it legitimately has zero sales invoices.
        # Without this the empty list reads as "this contact has no invoices".
        # Only an unnarrowed first page proves that, though: state, reference,
        # period or a page past the last one can each empty the result on their
        # own, so say which of those was in play rather than overstate it.
        narrowing = [
            label
            for label, active in (
                (f"state={state}", bool(state) and state != "all"),
                (f"reference={reference}", bool(reference)),
                (f"period={period}", bool(period)),
                (f"page={page}", page > 1),
            )
            if active
        ]
        if narrowing:
            opening = (
                f"No sales invoices for contact {contact_id} matching "
                f"{', '.join(narrowing)}. This does not establish that the contact "
                "has none at all -- widen the query before concluding that."
            )
        else:
            opening = f"Contact {contact_id} has no sales invoices."
        result["note"] = (
            f"{opening} If this contact is a supplier, zero sales invoices is "
            "expected: a supplier invoices you, so their documents are purchase "
            "invoices or receipts. Sales-invoice filters take contact_id but the "
            "document list endpoints do not, so use "
            "review_purchase_invoices(contact_id=...) for a supplier's history, "
            "or get_purchase_invoice_by_reference when you have the exact "
            "supplier invoice number."
        )
    return result


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def audit_recent_sales_invoice_send_methods(
    limit: Annotated[int, Field(ge=1, le=200, description="How many recent sent invoices to audit.")] = 30,
    page_scan_limit: Annotated[int, Field(ge=1, le=50, description="Maximum invoice pages to scan while collecting them.")] = 10,
) -> dict[str, Any]:
    """Use this to inspect whether recent Moneybird sales invoices were sent manually, by scheduled e-mail, or by e-invoice delivery."""
    client = ctx.get_client()
    return build_recent_sales_invoice_send_method_audit(
        client,
        limit=limit,
        page_scan_limit=page_scan_limit,
    )


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_estimates(
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird estimates (offertes). filter accepts
    Moneybird query syntax such as state:open|late|accepted|rejected|billed. Fetch the full
    record with moneybird_request("estimates/<id>")."""
    client = ctx.get_client()
    estimates = client.list_estimates(
        limit=limit,
        page=page,
        filter=build_filter_string(filter=filter, period=period),
    )
    return {
        "estimates": [
            {
                "id": str(item.get("id")),
                "estimate_id": item.get("estimate_id"),
                "state": item.get("state"),
                "contact": document_contact_title(item),
                "estimate_date": item.get("estimate_date"),
                "total_price_incl_tax": item.get("total_price_incl_tax"),
                "url": api_url("estimates", str(item.get("id")), client.administration_id),
            }
            for item in estimates
        ],
        "page": page,
        "count": len(estimates),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_recurring_sales_invoices(
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird recurring sales invoice templates
    (periodieke facturen), e.g. to check frequency, next run date, or auto_send. Fetch the
    full record with moneybird_request("recurring_sales_invoices/<id>")."""
    client = ctx.get_client()
    records = client.list_recurring_sales_invoices(limit=limit, page=page, filter=filter)
    return {
        "recurring_sales_invoices": [
            {
                "id": str(item.get("id")),
                "contact": document_contact_title(item),
                "active": item.get("active"),
                "frequency_type": item.get("frequency_type"),
                "frequency": item.get("frequency"),
                "invoice_date": item.get("invoice_date"),
                "auto_send": item.get("auto_send"),
                "total_price_incl_tax": item.get("total_price_incl_tax"),
                "url": api_url(
                    "recurring_sales_invoices", str(item.get("id")), client.administration_id
                ),
            }
            for item in records
        ],
        "page": page,
        "count": len(records),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def get_invoice_defaults_for_contact(
    contact_id: Annotated[str, Field(description="Moneybird contact id; give this or customer_id.")] = "",
    customer_id: Annotated[str, Field(description="Human-facing customer number; give this or contact_id.")] = "",
) -> dict[str, Any]:
    """Use this when you want the default workflow, document style, identity, tax, ledger, and send settings inferred from a contact's latest invoice."""
    client = ctx.get_client()
    contact = resolve_contact_reference(
        client,
        contact_id=contact_id,
        customer_id=customer_id,
    )
    defaults = infer_contact_invoice_defaults(client, contact)
    return {
        "contact": {
            "id": str(contact["id"]),
            "customer_id": contact.get("customer_id"),
            "title": contact_title(contact),
        },
        "defaults": defaults,
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_sales_invoice_draft(
    contact_id: ContactId,
    details: Annotated[
        list[dict[str, Any]],
        Field(description="Invoice lines. Each dict: description, price (decimal string, incl/excl VAT follows the administration setting), and optional amount, tax_rate_id, ledger_account_id, product_id, period."),
    ],
    reference: Annotated[str, Field(description="Reference text shown on the invoice.")] = "",
    invoice_date: OptionalDateString = "",
    due_date: OptionalDateString = "",
    currency: Annotated[str, Field(description="ISO currency code.")] = "EUR",
) -> dict[str, Any]:
    """Use this to make a new draft sales invoice for a customer: write, bill, or charge for goods or services. Do not execute the write until the user explicitly confirms."""
    if not details:
        raise MoneybirdError("At least one invoice line is required.")

    client = ctx.get_client()
    client.get_contact(contact_id)  # Validate scope and bind the approval to this tenant.

    normalized_details = []
    for detail in details:
        normalized_details.append(
            clean_dict(
                {
                    "description": detail.get("description"),
                    "price": detail.get("price"),
                    "amount": detail.get("amount", "1"),
                    "tax_rate_id": detail.get("tax_rate_id"),
                    "ledger_account_id": detail.get("ledger_account_id"),
                    "product_id": detail.get("product_id"),
                    "period": detail.get("period"),
                }
            )
        )

    payload = clean_dict(
        {
            "contact_id": contact_id,
            "reference": reference,
            "invoice_date": invoice_date,
            "due_date": due_date,
            "currency": currency,
            "details_attributes": normalized_details,
        }
    )
    return stage_write(
        "create_sales_invoice_draft",
        summary=(
            f"Create draft sales invoice for contact {contact_id} "
            f"with {len(normalized_details)} line(s)"
        ),
        payload=payload,
        preview=payload,
    )


def _execute_create_sales_invoice_draft(client, payload: dict[str, Any]) -> dict[str, Any]:
    invoice = {key: value for key, value in payload.items() if key != "fingerprint"}
    mark_write_dispatch_started()
    created = client.create_sales_invoice(invoice)
    record_id = str(created.get("id") or "")
    if not record_id:
        raise MoneybirdError(
            "Moneybird did not return a sales invoice id; reconcile before retrying."
        )
    mark_write_verifying()
    record = client.get_sales_invoice(record_id)
    verification = verify_sales_invoice_payload(invoice, record)
    record_id_matches = str(record.get("id") or "") == record_id
    fully_verified = record_id_matches and verification["fully_verified"]
    return {
        "_status": (
            "created" if fully_verified else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "sales_invoice_id": record_id,
            "contact_id": record.get("contact_id"),
            "reference": record.get("reference"),
            "fully_verified": fully_verified,
        },
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "contact_id": record.get("contact_id"),
            "reference": record.get("reference"),
            "total_price_incl_tax": record.get("total_price_incl_tax"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
        "verification": {
            "independent_post_read": True,
            "record_id_matches": record_id_matches,
            **verification,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_sales_invoice_draft_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared draft invoice creation."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "create_sales_invoice_draft",
        _execute_create_sales_invoice_draft,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_send_sales_invoice(
    sales_invoice_id: SalesInvoiceId,
    sending_scheduled: Annotated[bool, Field(description="True = schedule the send instead of sending immediately.")] = False,
    invoice_date: OptionalDateString = "",
    delivery_method: Annotated[str, Field(description="'Email', 'Simplerinvoicing', 'Post', or 'Manual'. Empty = the contact's configured method.")] = "",
    email_address: Annotated[str, Field(description="Override recipient email; empty = the contact's invoice email.")] = "",
    email_message: Annotated[str, Field(description="Custom message for the invoice email body.")] = "",
) -> dict[str, Any]:
    """Use this to send an invoice to a customer by email or post, now or scheduled for later. Do not execute the send until the user explicitly confirms. Scheduled sends automatically include a merge-compatibility check against other invoices already planned for that contact/date."""
    if sending_scheduled and not invoice_date:
        raise MoneybirdError(
            "invoice_date is required when sending_scheduled is true."
        )

    client = ctx.get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    payload = clean_dict(
        {
            "sending_scheduled": sending_scheduled,
            "invoice_date": invoice_date,
            "delivery_method": delivery_method,
            "email_address": email_address,
            "email_message": email_message,
        }
    )
    summary = (
        f"Send sales invoice {sales_invoice_id} now"
        if not sending_scheduled
        else f"Schedule sales invoice {sales_invoice_id} for {invoice_date}"
    )
    approval = stage_write(
        "send_sales_invoice",
        summary=summary,
        payload={
            "sales_invoice_id": sales_invoice_id,
            "sales_invoice_sending": payload,
            "expected_record": _invoice_precondition_snapshot(record),
        },
        preview={
            "sales_invoice_id": sales_invoice_id,
            "sales_invoice_sending": payload,
        },
    )
    merge_check = {
        "checked": False,
        "status": "not_scheduled",
        "summary": "No automatic merge check because this invoice is not scheduled.",
    }
    if sending_scheduled:
        candidates = list_scheduled_merge_candidates(
            client,
            contact_id=str(record.get("contact_id") or record.get("contact", {}).get("id") or ""),
            scheduled_send_on=invoice_date,
            exclude_sales_invoice_id=sales_invoice_id,
        )
        merge_check = evaluate_merge_compatibility(
            build_merge_snapshot_from_invoice(
                record,
                scheduled_send_on=invoice_date,
            ),
            candidates,
        )
    approval["merge_check"] = merge_check
    return approval


def _execute_send_sales_invoice(client, payload: dict[str, Any]) -> dict[str, Any]:
    sales_invoice_id = payload["sales_invoice_id"]
    before = client.get_sales_invoice(sales_invoice_id)
    changed_preconditions = _changed_invoice_preconditions(
        before,
        payload.get("expected_record") or {},
    )
    if str(before.get("id") or "") != str(sales_invoice_id):
        changed_preconditions["id"] = {
            "expected": sales_invoice_id,
            "actual": before.get("id"),
        }
    if changed_preconditions:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {
                "sales_invoice_id": sales_invoice_id,
                "precondition_failed": True,
            },
            "verification": {
                "write_dispatched": False,
                "changed_preconditions": changed_preconditions,
            },
        }

    mark_write_dispatch_started()
    client.send_sales_invoice(
        sales_invoice_id,
        payload["sales_invoice_sending"],
    )
    mark_write_verifying()
    record = client.get_sales_invoice(sales_invoice_id)
    record_id = str(record.get("id") or "")
    sending = payload["sales_invoice_sending"]
    if sending.get("sending_scheduled"):
        fully_verified = (
            record_id == str(sales_invoice_id)
            and str(record.get("state") or "") == "scheduled"
            and str(record.get("invoice_date") or "")
            == str(sending.get("invoice_date") or "")
        )
    else:
        fully_verified = record_id == str(sales_invoice_id) and (
            bool(record.get("sent_at"))
            or str(record.get("state") or "") not in {"", "draft"}
        )
    return {
        "_status": (
            "sent_or_scheduled"
            if fully_verified
            else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "sales_invoice_id": record_id,
            "state": record.get("state"),
            "invoice_date": record.get("invoice_date"),
            "fully_verified": fully_verified,
        },
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "invoice_date": record.get("invoice_date"),
            "sent_at": record.get("sent_at"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
        "verification": {
            "independent_post_read": True,
            "sending_scheduled": bool(sending.get("sending_scheduled")),
            "record_id_matches": record_id == str(sales_invoice_id),
            "fully_verified": fully_verified,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def send_sales_invoice_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared invoice send action."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "send_sales_invoice", _execute_send_sales_invoice
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_pause_sales_invoice_workflow(sales_invoice_id: SalesInvoiceId) -> dict[str, Any]:
    """Use this before pausing a sales invoice workflow. This is the safe way to stop a scheduled send from going out automatically."""
    client = ctx.get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    return stage_write(
        "pause_sales_invoice_workflow",
        summary=f"Pause workflow for sales invoice {record.get('invoice_id') or record.get('id')}",
        payload={
            "sales_invoice_id": sales_invoice_id,
            "expected_record": _invoice_precondition_snapshot(record),
        },
        preview={"sales_invoice_id": sales_invoice_id, "state": record.get("state")},
    )


def _workflow_state_result(
    client,
    record: dict[str, Any],
    status: str,
    *,
    expected_record_id: str,
) -> dict[str, Any]:
    record_id = str(record.get("id") or "")
    expected_paused = status == "paused"
    record_id_matches = record_id == str(expected_record_id)
    fully_verified = (
        record_id_matches and record.get("paused") is expected_paused
    )
    return {
        "_status": status if fully_verified else "completed_with_verification_errors",
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "sales_invoice_id": record_id,
            "state": record.get("state"),
            "paused": record.get("paused"),
            "fully_verified": fully_verified,
        },
        "sales_invoice": {
            "id": record_id,
            "invoice_id": record.get("invoice_id"),
            "state": record.get("state"),
            "paused": record.get("paused"),
            "url": api_url("sales_invoices", record_id, client.administration_id),
        },
        "verification": {
            "independent_post_read": True,
            "expected_paused": expected_paused,
            "record_id_matches": record_id_matches,
            "paused_matches": record.get("paused") is expected_paused,
        },
    }


def _execute_workflow_state_change(
    client,
    payload: dict[str, Any],
    *,
    pause: bool,
) -> dict[str, Any]:
    sales_invoice_id = payload["sales_invoice_id"]
    before = client.get_sales_invoice(sales_invoice_id)
    changed_preconditions = _changed_invoice_preconditions(
        before,
        payload.get("expected_record") or {},
    )
    if str(before.get("id") or "") != str(sales_invoice_id):
        changed_preconditions["id"] = {
            "expected": sales_invoice_id,
            "actual": before.get("id"),
        }
    if changed_preconditions:
        return {
            "_status": "precondition_failed",
            "_audit_result": "failed_pre_write",
            "_audit": {
                "sales_invoice_id": sales_invoice_id,
                "precondition_failed": True,
            },
            "verification": {
                "write_dispatched": False,
                "changed_preconditions": changed_preconditions,
            },
        }
    if pause:
        mark_write_dispatch_started()
        client.pause_sales_invoice(sales_invoice_id)
        status = "paused"
    else:
        mark_write_dispatch_started()
        client.resume_sales_invoice(sales_invoice_id)
        status = "resumed"
    mark_write_verifying()
    after = client.get_sales_invoice(sales_invoice_id)
    return _workflow_state_result(
        client,
        after,
        status,
        expected_record_id=sales_invoice_id,
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def pause_sales_invoice_workflow_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed pausing the invoice workflow."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "pause_sales_invoice_workflow",
        lambda client, payload: _execute_workflow_state_change(
            client,
            payload,
            pause=True,
        ),
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_resume_sales_invoice_workflow(sales_invoice_id: SalesInvoiceId) -> dict[str, Any]:
    """Use this before resuming a previously paused sales invoice workflow."""
    client = ctx.get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    return stage_write(
        "resume_sales_invoice_workflow",
        summary=f"Resume workflow for sales invoice {record.get('invoice_id') or record.get('id')}",
        payload={
            "sales_invoice_id": sales_invoice_id,
            "expected_record": _invoice_precondition_snapshot(record),
        },
        preview={"sales_invoice_id": sales_invoice_id, "state": record.get("state")},
    )


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def resume_sales_invoice_workflow_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed resuming the invoice workflow."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "resume_sales_invoice_workflow",
        lambda client, payload: _execute_workflow_state_change(
            client,
            payload,
            pause=False,
        ),
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_create_credit_invoice(sales_invoice_id: SalesInvoiceId) -> dict[str, Any]:
    """Use this to credit, refund, cancel, or reverse a sales invoice: Moneybird duplicates it
    into a new DRAFT credit invoice with negated amounts. Nothing is sent automatically; sending
    the credit invoice afterwards needs its own prepare_send_sales_invoice approval. Do not
    execute the write until the user explicitly confirms."""
    client = ctx.get_client()
    record = client.get_sales_invoice(sales_invoice_id)
    original_details = [
        clean_dict(
            {
                key: detail.get(key)
                for key in SALES_INVOICE_LINE_FIELDS
                if key != "id"
            }
        )
        for detail in record.get("details") or []
    ]
    expected_credit_details: list[dict[str, Any]] = []
    for detail in original_details:
        expected = dict(detail)
        if "price" in expected:
            expected["price"] = str(-money_decimal(expected["price"]))
        expected_credit_details.append(expected)
    original_contact_id = str(
        record.get("contact_id") or (record.get("contact") or {}).get("id") or ""
    )
    return stage_write(
        "create_credit_invoice",
        summary=f"Create draft credit invoice for {invoice_title(record)}",
        payload={
            "sales_invoice_id": str(sales_invoice_id),
            "total_original": str(record.get("total_price_incl_tax") or "0"),
            "expected_original": {
                **_invoice_precondition_snapshot(record),
                "total_price_incl_tax": str(
                    record.get("total_price_incl_tax") or "0"
                ),
                "details": original_details,
            },
            "expected_credit_invoice": clean_dict(
                {
                    "contact_id": original_contact_id,
                    "currency": record.get("currency"),
                    "details_attributes": expected_credit_details,
                }
            ),
        },
        preview={
            "original_invoice": {
                "id": str(record.get("id")),
                "title": invoice_title(record),
                "contact": document_contact_title(record),
                "state": record.get("state"),
                "invoice_date": record.get("invoice_date"),
                "total_price_incl_tax": record.get("total_price_incl_tax"),
                "url": api_url(
                    "sales_invoices", str(record.get("id")), client.administration_id
                ),
            },
            "effect": (
                "A new draft credit invoice is created with the same lines and negated amounts. "
                "It is not sent and the original invoice is not changed."
            ),
        },
        fingerprint=duplicate_fingerprint(
            "create_credit_invoice",
            {
                "sales_invoice_id": str(sales_invoice_id),
                "original_occurrence": _invoice_precondition_snapshot(record),
            },
        ),
    )


def _execute_create_credit_invoice(client, payload: dict[str, Any]) -> dict[str, Any]:
    original = client.get_sales_invoice(payload["sales_invoice_id"])
    expected_original = payload.get("expected_original") or {}
    changed_original = {
        key: {"expected": value, "actual": original.get(key)}
        for key, value in expected_original.items()
        if key != "details"
        and (
            money_decimal(original.get(key) or 0) != money_decimal(value or 0)
            if key == "total_price_incl_tax"
            else str(original.get(key) or "") != str(value or "")
        )
    }
    original_line_mismatches = compare_controlled_rows(
        expected_original.get("details"),
        original.get("details"),
        fields=SALES_INVOICE_LINE_FIELDS,
        decimal_fields=SALES_INVOICE_LINE_DECIMALS,
    )
    if str(original.get("id") or "") != str(payload["sales_invoice_id"]):
        changed_original["id"] = {
            "expected": payload["sales_invoice_id"],
            "actual": original.get("id"),
        }
    if changed_original or original_line_mismatches:
        raise MoneybirdError(
            "The original sales invoice changed after the credit preview. Prepare again."
        )
    mark_write_dispatch_started()
    created = client.duplicate_sales_invoice_to_credit_invoice(
        payload["sales_invoice_id"]
    )
    record_id = str(created.get("id") or "")
    if not record_id:
        raise MoneybirdError(
            "Moneybird did not return a credit invoice id; reconcile before retrying."
        )
    mark_write_verifying()
    record = client.get_sales_invoice(record_id)
    total_credit = money_decimal(record.get("total_price_incl_tax") or 0)
    total_original = money_decimal(payload["total_original"])
    payload_verification = verify_sales_invoice_payload(
        payload.get("expected_credit_invoice") or {},
        record,
    )
    state_is_draft = str(record.get("state") or "") == "draft"
    record_id_matches = str(record.get("id") or "") == record_id
    fully_verified = (
        total_credit == -total_original
        and state_is_draft
        and record_id_matches
        and payload_verification["fully_verified"]
    )
    return {
        "_status": (
            "created" if fully_verified else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "original_sales_invoice_id": payload["sales_invoice_id"],
            "credit_invoice_id": str(record.get("id")),
            "credit_negates_original": fully_verified,
            "state_is_draft": state_is_draft,
        },
        "credit_invoice": {
            "id": str(record.get("id")),
            "state": record.get("state"),
            "reference": record.get("reference"),
            "total_price_incl_tax": record.get("total_price_incl_tax"),
            "url": api_url(
                "sales_invoices", str(record.get("id")), client.administration_id
            ),
        },
        "verification": {
            "total_original": str(total_original),
            "total_credit": str(total_credit),
            "credit_negates_original": fully_verified,
            "state_is_draft": state_is_draft,
            "record_id_matches": record_id_matches,
            "payload_verification": payload_verification,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def create_credit_invoice_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared credit invoice."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "create_credit_invoice", _execute_create_credit_invoice
    )


