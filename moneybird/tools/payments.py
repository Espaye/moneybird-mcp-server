"""Guarded payment registration on sales invoices, purchase invoices, and receipts."""
from __future__ import annotations

from typing import Any

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
from ._registry import mcp
from ._writes import run_approved_write, stage_write
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


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_register_payment(
    document_type: str,
    document_id: str,
    payment_date: str,
    price: str,
    financial_account_id: str = "",
    financial_mutation_id: str = "",
    transaction_identifier: str = "",
    manual_payment_action: str = "",
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
    return stage_write(
        "register_payment",
        summary=f"Register payment of {amount} on {summary['title']}",
        payload={
            "document_type": kind,
            "document_id": str(document_id),
            "payment": payment,
            "total_before": str(record.get("total_price_incl_tax") or "0"),
        },
        preview={
            "document": summary,
            "payment": payment,
            "warnings": warnings,
        },
        fingerprint=duplicate_fingerprint(
            "register_payment",
            {"document_type": kind, "document_id": str(document_id), "payment": payment},
        ),
    )


def _execute_register_payment(client, payload: dict[str, Any]) -> dict[str, Any]:
    kind = payload["document_type"]
    document_id = payload["document_id"]
    if kind == "sales_invoice":
        client.register_sales_invoice_payment(document_id, payload["payment"])
    else:
        client.register_document_payment(kind, document_id, payload["payment"])
    record = _fetch_payable_record(client, kind, document_id)
    summary = _payable_record_summary(client, kind, record)
    total_after = str(record.get("total_price_incl_tax") or "0")
    total_unchanged = money_decimal(total_after) == money_decimal(payload["total_before"])
    payment_present = any(
        str(item.get("payment_date")) == payload["payment"]["payment_date"]
        and money_decimal(item.get("price") or 0) == money_decimal(payload["payment"]["price"])
        for item in record.get("payments") or []
    )
    return {
        "_status": "payment_registered",
        "_audit": {
            "document_type": kind,
            "document_id": str(document_id),
            "price": payload["payment"]["price"],
            "payment_date": payload["payment"]["payment_date"],
        },
        "document": summary,
        "verification": {
            "total_before": payload["total_before"],
            "total_after": total_after,
            "total_unchanged_to_the_cent": total_unchanged,
            "payment_visible_on_document": payment_present,
            "open_amount_after": summary["open_amount"],
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def register_payment_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared payment registration."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "register_payment", _execute_register_payment
    )


