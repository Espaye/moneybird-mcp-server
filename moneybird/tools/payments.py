"""Guarded payment registration on sales invoices, purchase invoices, and receipts."""
from __future__ import annotations

from collections import Counter
from typing import Annotated, Any

from pydantic import Field

from ..config import (
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from ..formatting import (
    api_url,
    clean_dict,
    document_contact_title,
    money_decimal,
    normalize_document_kind,
    document_url,
    duplicate_fingerprint,
    invoice_title,
    iso_now,
    purchase_document_title,
)
from ..invoicing import (
    parse_decimal_number,
)
from ._params import (
    ApprovalId,
    DateString,
    PayableDocumentType,
    PriceString,
)
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)
from . import _context as ctx


def _fetch_payable_record(client, document_type: str, document_id: str) -> dict[str, Any]:
    if document_type == "sales_invoice":
        return client.get_sales_invoice(document_id)
    return client.get_document(document_type, document_id)


def _normalize_payable_document_type(document_type: str) -> str:
    kind = str(document_type).strip().lower()
    if kind in {"sales_invoice", "sales_invoices"}:
        return "sales_invoice"
    kind = normalize_document_kind(kind)
    if kind not in {"purchase_invoice", "receipt"}:
        raise MoneybirdError(
            "document_type must be sales_invoice, purchase_invoice, or receipt."
        )
    return kind


def _open_amount(record: dict[str, Any]) -> str:
    """Open amount of an invoice/document: total_unpaid when present, else total minus payments."""
    if record.get("total_unpaid") is not None:
        return str(record["total_unpaid"])
    total = money_decimal(record.get("total_price_incl_tax") or 0)
    paid = sum(
        (money_decimal(payment.get("price") or 0) for payment in record.get("payments") or []),
        start=money_decimal("0"),
    )
    return str(total - paid)


def _payable_record_summary(
    client, document_type: str, record: dict[str, Any]
) -> dict[str, Any]:
    record_id = str(record.get("id"))
    if document_type == "sales_invoice":
        title = invoice_title(record)
        url = api_url("sales_invoices", record_id, client.administration_id)
    else:
        title = purchase_document_title(document_type, record)
        url = document_url(document_type, record_id, client.administration_id)
    return {
        "id": record_id,
        "document_type": document_type,
        "title": title,
        "contact": document_contact_title(record),
        "state": record.get("state"),
        "date": record.get("invoice_date") or record.get("date"),
        "total_price_incl_tax": record.get("total_price_incl_tax"),
        "open_amount": _open_amount(record),
        "url": url,
    }


def _payment_key(payment: dict[str, Any]) -> tuple[str, ...]:
    amount = money_decimal(payment.get("price") or 0)
    return (
        str(payment.get("payment_date") or ""),
        format(amount.normalize(), "f"),
        str(payment.get("financial_account_id") or ""),
        str(payment.get("financial_mutation_id") or ""),
        str(payment.get("transaction_identifier") or ""),
        str(payment.get("manual_payment_action") or ""),
    )


def _payment_keys(record: dict[str, Any]) -> list[list[str]]:
    return [
        list(_payment_key(payment))
        for payment in record.get("payments") or []
        if isinstance(payment, dict)
    ]


def _payment_precondition(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "version": str(record.get("version") or ""),
        "updated_at": str(record.get("updated_at") or ""),
        "total_price_incl_tax": str(record.get("total_price_incl_tax") or "0"),
        "open_amount": _open_amount(record),
        "payment_keys": _payment_keys(record),
    }


def _assert_payment_precondition(
    record: dict[str, Any],
    expected: dict[str, Any],
    *,
    document_id: str,
) -> None:
    if not expected:
        raise MoneybirdError(
            "This payment approval predates exact payment preconditions. Prepare it again."
        )
    current = _payment_precondition(record)
    for field in ("version", "updated_at"):
        expected_value = str(expected.get(field) or "")
        if expected_value and str(current.get(field) or "") != expected_value:
            raise MoneybirdError(
                f"Document {document_id} changed after the payment preview "
                f"({field} {expected_value} -> {current.get(field)}). Prepare again."
            )
    monetary_fields = ("total_price_incl_tax", "open_amount")
    for field in monetary_fields:
        if money_decimal(current[field]) != money_decimal(expected[field]):
            raise MoneybirdError(
                f"Document {document_id} {field} changed after the payment preview. "
                "Prepare again."
            )
    if current["payment_keys"] != expected.get("payment_keys"):
        raise MoneybirdError(
            f"Document {document_id} payments changed after the preview. Prepare again."
        )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_register_payment(
    document_type: PayableDocumentType,
    document_id: Annotated[
        str,
        Field(description="Id of the sales invoice, purchase invoice, or receipt (matching document_type)."),
    ],
    payment_date: DateString,
    price: PriceString,
    financial_account_id: Annotated[
        str,
        Field(description="Optional financial account the payment came from/went to, e.g. from list_financial_accounts."),
    ] = "",
    financial_mutation_id: Annotated[
        str,
        Field(description="Optional bank mutation id to associate; prefer prepare_link_bank_mutation_booking when the mutation exists."),
    ] = "",
    transaction_identifier: Annotated[
        str,
        Field(description="Optional bank transaction reference to store on the payment."),
    ] = "",
    manual_payment_action: Annotated[
        str,
        Field(description="Optional Moneybird manual_payment_action, e.g. 'private_payment', 'cash_payment', 'payment_without_proof', 'rounding_error'."),
    ] = "",
) -> dict[str, Any]:
    """Use this before registering a payment on a sales invoice, purchase invoice, or receipt
    (mark it fully or partially paid). document_type is sales_invoice, purchase_invoice, or
    receipt. Prefer linking the actual bank mutation instead (prepare_link_bank_mutation_booking)
    when one exists; use this for payments outside the bank feed (cash, private, foreign PSP).
    Do not execute the write until the user explicitly confirms."""
    kind = _normalize_payable_document_type(document_type)
    if not payment_date.strip():
        raise MoneybirdError("payment_date is required (YYYY-MM-DD).")
    amount = parse_decimal_number(price, label="price")
    if amount <= 0:
        raise MoneybirdError("price must be greater than zero.")

    client = ctx.get_client()
    record = _fetch_payable_record(client, kind, document_id)
    summary = _payable_record_summary(client, kind, record)

    warnings: list[str] = []
    open_amount = money_decimal(summary["open_amount"])
    if amount > open_amount:
        warnings.append(
            f"Payment {amount} is higher than the open amount {open_amount}."
        )
    elif amount != open_amount:
        warnings.append(
            f"Partial payment: {amount} of open amount {open_amount}; the document stays partly open."
        )
    if not financial_account_id and not financial_mutation_id and not manual_payment_action:
        warnings.append(
            "No financial_account_id, financial_mutation_id, or manual_payment_action given; "
            "Moneybird will book this as a plain manual payment."
        )

    payment = clean_dict(
        {
            "payment_date": payment_date.strip(),
            "price": str(amount),
            "financial_account_id": financial_account_id.strip(),
            "financial_mutation_id": financial_mutation_id.strip(),
            "transaction_identifier": transaction_identifier.strip(),
            "manual_payment_action": manual_payment_action.strip(),
        }
    )
    precondition = _payment_precondition(record)
    fingerprint_payload = {
        "document_type": kind,
        "document_id": str(document_id),
        "payment": payment,
        "precondition": precondition,
    }
    return stage_write(
        "register_payment",
        summary=f"Register payment of {amount} on {summary['title']}",
        payload={
            "document_type": kind,
            "document_id": str(document_id),
            "payment": payment,
            "total_before": str(record.get("total_price_incl_tax") or "0"),
            "precondition": precondition,
        },
        preview={
            "document": summary,
            "payment": payment,
            "warnings": warnings,
        },
        fingerprint=duplicate_fingerprint(
            "register_payment",
            fingerprint_payload,
        ),
    )


def _execute_register_payment(client, payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload["document_type"]
    document_id = payload["document_id"]
    before = _fetch_payable_record(client, kind, document_id)
    if str(before.get("id") or "") != str(document_id):
        raise MoneybirdError(
            f"Document {document_id} lookup returned a different record. Prepare again."
        )
    _assert_payment_precondition(
        before,
        payload.get("precondition") or {},
        document_id=document_id,
    )
    mark_write_dispatch_started()
    if kind == "sales_invoice":
        client.register_sales_invoice_payment(document_id, payload["payment"])
    else:
        client.register_document_payment(kind, document_id, payload["payment"])
    mark_write_verifying()
    record = _fetch_payable_record(client, kind, document_id)
    summary = _payable_record_summary(client, kind, record)
    record_id_matches = str(record.get("id") or "") == str(document_id)
    total_after = str(record.get("total_price_incl_tax") or "0")
    total_unchanged = money_decimal(total_after) == money_decimal(payload["total_before"])
    before_counter = Counter(
        tuple(key)
        for key in payload["precondition"]["payment_keys"]
    )
    after_counter = Counter(tuple(key) for key in _payment_keys(record))
    added_payments = after_counter - before_counter
    removed_payments = before_counter - after_counter
    requested_key = _payment_key(payload["payment"])
    exact_payment_delta = (
        not removed_payments
        and sum(added_payments.values()) == 1
        and added_payments[requested_key] == 1
    )
    expected_open_after = (
        money_decimal(payload["precondition"]["open_amount"])
        - money_decimal(payload["payment"]["price"])
    )
    open_amount_after = money_decimal(summary["open_amount"])
    open_amount_delta_matches = open_amount_after == expected_open_after
    fully_verified = (
        record_id_matches
        and total_unchanged
        and exact_payment_delta
        and open_amount_delta_matches
    )
    return {
        "_status": (
            "payment_registered"
            if fully_verified
            else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "document_type": kind,
            "document_id": str(document_id),
            "price": payload["payment"]["price"],
            "payment_date": payload["payment"]["payment_date"],
            "total_unchanged_to_the_cent": total_unchanged,
            "record_id_matches": record_id_matches,
            "exact_new_payment_delta": exact_payment_delta,
            "open_amount_delta_matches": open_amount_delta_matches,
        },
        "document": summary,
        "verification": {
            "total_before": payload["total_before"],
            "total_after": total_after,
            "record_id_matches": record_id_matches,
            "total_unchanged_to_the_cent": total_unchanged,
            "payment_visible_on_document": exact_payment_delta,
            "exact_new_payment_delta": exact_payment_delta,
            "payments_added": [
                {
                    "payment_date": key[0],
                    "price": key[1],
                    "financial_account_id": key[2] or None,
                    "financial_mutation_id": key[3] or None,
                    "transaction_identifier": key[4] or None,
                    "manual_payment_action": key[5] or None,
                    "count": count,
                }
                for key, count in sorted(added_payments.items())
            ],
            "payments_removed": [
                {
                    "payment_date": key[0],
                    "price": key[1],
                    "financial_account_id": key[2] or None,
                    "financial_mutation_id": key[3] or None,
                    "transaction_identifier": key[4] or None,
                    "manual_payment_action": key[5] or None,
                    "count": count,
                }
                for key, count in sorted(removed_payments.items())
            ],
            "expected_open_amount_after": str(expected_open_after),
            "open_amount_delta_matches": open_amount_delta_matches,
            "open_amount_after": summary["open_amount"],
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def register_payment_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared payment registration."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "register_payment", _execute_register_payment
    )


