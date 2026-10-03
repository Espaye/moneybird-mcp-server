"""Guarded payment registration on sales invoices, purchase invoices, and receipts."""
from __future__ import annotations

from collections import Counter
from typing import Annotated, Any

from pydantic import Field

from ..config import (
    PREPARE_ANNOTATIONS,
    MoneybirdError,
)
from ..formatting import (
    api_url,
    clean_dict,
    document_contact_title,
    document_url,
    duplicate_fingerprint,
    invoice_title,
    money_decimal,
    normalize_document_kind,
    purchase_document_title,
)
from ..invoicing import (
    parse_decimal_number,
)
from . import _context as ctx
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


# Payment fields the request can set, as Moneybird reports them back. The before/after
# multiset compares all of them, so a payment moved to another ledger is a changed
# payment. invoice_id is left out: on a recorded payment it is the document the payment
# belongs to, not the request's settlement target.
_PAYMENT_KEY_FIELDS = (
    "payment_date",
    "price",
    "financial_account_id",
    "financial_mutation_id",
    "transaction_identifier",
    "manual_payment_action",
    "ledger_account_id",
)


def _payment_key(payment: dict[str, Any]) -> tuple[str, ...]:
    amount = money_decimal(payment.get("price") or 0)
    return tuple(
        format(amount.normalize(), "f")
        if field == "price"
        else str(payment.get(field) or "")
        for field in _PAYMENT_KEY_FIELDS
    )


def _payment_key_matches_request(
    key: tuple[str, ...], requested: tuple[str, ...]
) -> bool:
    """True when the recorded payment carries every field the request set.

    Fields the request left empty are not compared: Moneybird fills some of them in
    itself (a plain payment comes back with the creditor or debtor ledger account).
    """
    return all(
        not want or have == want for have, want in zip(key, requested, strict=True)
    )


def _payment_key_summary(key: tuple[str, ...], count: int) -> dict[str, Any]:
    return {
        **{field: value or None for field, value in zip(_PAYMENT_KEY_FIELDS, key)},
        "count": count,
    }


_BALANCE_SETTLEMENT = "balance_settlement"
_INVOICES_SETTLEMENT = "invoices_settlement"


def _check_balance_ledger_account(client, ledger_account_id: str) -> dict[str, Any]:
    ledger = client.get_ledger_account(ledger_account_id)
    if ledger.get("active") is False:
        raise MoneybirdError(f"Ledger account {ledger_account_id} is inactive.")
    allowed_types = set(ledger.get("allowed_document_types") or [])
    # Moneybird's API accepts a balance settlement on any ledger account (verified
    # 2026-10-04), but only accounts that allow payments are offered for it in
    # Moneybird itself, so anything else is very likely the wrong account.
    if "payment" not in allowed_types:
        raise MoneybirdError(
            f"Ledger account {ledger_account_id} ({ledger.get('name')}) does not "
            "allow payments, so it is not meant for settling invoices. Choose a "
            "balance account that allows payments, or allow payments on this one in "
            "Moneybird's ledger account settings first."
        )
    return {
        "id": str(ledger.get("id") or ledger_account_id),
        "name": ledger.get("name"),
        "account_type": ledger.get("account_type"),
    }


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
        Field(description="Optional Moneybird manual_payment_action, e.g. 'private_payment', 'cash_payment', 'payment_without_proof', 'rounding_error', 'balance_settlement'."),
    ] = "",
    ledger_account_id: Annotated[
        str,
        Field(description="Balance ledger account to settle against (manual_payment_action 'balance_settlement'), e.g. a payment-processor clearing account or prepaid credit. The account must allow payments."),
    ] = "",
) -> dict[str, Any]:
    """Use this to record (register) a payment on a sales invoice, purchase invoice, or receipt
    (mark it fully or partially paid). document_type is sales_invoice, purchase_invoice, or
    receipt. Prefer linking the actual bank mutation instead (prepare_link_bank_mutation_booking)
    when one exists; use this for payments outside the bank feed (cash, private, foreign PSP).
    To settle against a balance account instead of money (fees withheld by a payment
    processor, prepaid credit), pass ledger_account_id; the action becomes
    balance_settlement. Do not execute the write until the user explicitly confirms."""
    kind = _normalize_payable_document_type(document_type)
    if not payment_date.strip():
        raise MoneybirdError("payment_date is required (YYYY-MM-DD).")
    amount = parse_decimal_number(price, label="price")
    if amount <= 0:
        raise MoneybirdError("price must be greater than zero.")
    action = manual_payment_action.strip()
    ledger_account_id = ledger_account_id.strip()
    if action == _INVOICES_SETTLEMENT:
        raise MoneybirdError(
            "invoices_settlement is not supported yet: Moneybird books the opposite "
            "payment on the other document itself, which this tool cannot verify."
        )
    if ledger_account_id and not action:
        action = _BALANCE_SETTLEMENT
    if ledger_account_id and action != _BALANCE_SETTLEMENT:
        raise MoneybirdError(
            "ledger_account_id is only used with manual_payment_action "
            f"'{_BALANCE_SETTLEMENT}', not '{action}'."
        )
    if action == _BALANCE_SETTLEMENT and not ledger_account_id:
        raise MoneybirdError(
            f"manual_payment_action '{_BALANCE_SETTLEMENT}' needs ledger_account_id."
        )

    client = ctx.get_client()
    record = _fetch_payable_record(client, kind, document_id)
    summary = _payable_record_summary(client, kind, record)
    settlement_ledger = (
        _check_balance_ledger_account(client, ledger_account_id)
        if ledger_account_id
        else None
    )

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
    if not financial_account_id and not financial_mutation_id and not action:
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
            "manual_payment_action": action,
            "ledger_account_id": ledger_account_id,
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
            **(
                {"settlement_ledger_account": settlement_ledger}
                if settlement_ledger
                else {}
            ),
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
        and all(
            _payment_key_matches_request(key, requested_key)
            for key in added_payments
        )
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
                _payment_key_summary(key, count)
                for key, count in sorted(added_payments.items())
            ],
            "payments_removed": [
                _payment_key_summary(key, count)
                for key, count in sorted(removed_payments.items())
            ],
            "expected_open_amount_after": str(expected_open_after),
            "open_amount_delta_matches": open_amount_delta_matches,
            "open_amount_after": summary["open_amount"],
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def register_payment_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared payment registration."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "register_payment", _execute_register_payment
    )


