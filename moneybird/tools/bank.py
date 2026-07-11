"""Bank/cash mutations: reads plus guarded link/unlink of bookings (manual reconciliation)."""
from __future__ import annotations

from typing import Any

from ..config import (
    FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
    FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES,
    MoneybirdError,
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from ..formatting import (
    clean_dict,
    document_contact_title,
    compact_financial_mutation_summary,
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
from .payments import _open_amount
from . import _context as ctx


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_financial_mutations(
    limit: int = 10,
    page: int = 1,
    filter: str = "",
    period: str = "",
) -> dict[str, Any]:
    """Use this when you need a compact list of Moneybird bank or cash mutations."""
    client = ctx.get_client()
    mutations = client.list_financial_mutations(
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    return {
        "financial_mutations": [
            compact_financial_mutation_summary(item, client.administration_id)
            for item in mutations
        ],
        "page": page,
        "count": len(mutations),
    }


def _link_target_summary(client, booking_type: str, booking_id: str) -> dict[str, Any]:
    """Best-effort lookup of the booking target so the preview names what gets linked."""
    try:
        if booking_type == "SalesInvoice":
            record = client.get_sales_invoice(booking_id)
            return {
                "title": invoice_title(record),
                "contact": document_contact_title(record),
                "total_price_incl_tax": record.get("total_price_incl_tax"),
                "open_amount": _open_amount(record),
                "state": record.get("state"),
            }
        if booking_type == "LedgerAccount":
            record = client.get_ledger_account(booking_id)
            return {
                "title": record.get("name"),
                "account_type": record.get("account_type"),
            }
        if booking_type == "Document":
            for kind in ("purchase_invoice", "receipt", "general_journal_document"):
                try:
                    record = client.get_document(kind, booking_id)
                except MoneybirdError:
                    continue
                return {
                    "title": purchase_document_title(kind, record),
                    "document_kind": kind,
                    "contact": document_contact_title(record),
                    "total_price_incl_tax": record.get("total_price_incl_tax"),
                    "open_amount": _open_amount(record),
                    "state": record.get("state"),
                }
    except MoneybirdError as exc:
        return {"lookup_error": str(exc)}
    return {"note": f"No preview lookup implemented for booking_type {booking_type}."}


def _mutation_link_state(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": record.get("state"),
        "amount": record.get("amount"),
        "amount_open": record.get("amount_open"),
        "payments": [
            {
                "id": str(item.get("id")),
                "price": item.get("price"),
                "invoice_id": str(item.get("invoice_id") or "") or None,
                "invoice_type": item.get("invoice_type"),
            }
            for item in record.get("payments") or []
        ],
        "ledger_account_bookings": [
            {
                "id": str(item.get("id")),
                "ledger_account_id": str(item.get("ledger_account_id") or "") or None,
                "price": item.get("price"),
                "description": item.get("description"),
            }
            for item in record.get("ledger_account_bookings") or []
        ],
    }


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_link_bank_mutation_booking(
    financial_mutation_id: str,
    booking_type: str,
    booking_id: str,
    price: str = "",
) -> dict[str, Any]:
    """Use this before linking a bank/cash mutation to a booking: an open invoice or document
    (booking_type SalesInvoice or Document) or directly to a ledger category (LedgerAccount).
    This is the manual counterpart of Moneybird's bank reconciliation. Leave price empty to let
    Moneybird link the full open amount. Do not execute the write until the user explicitly
    confirms."""
    booking_type = str(booking_type).strip()
    if booking_type not in FINANCIAL_MUTATION_LINK_BOOKING_TYPES:
        supported = ", ".join(sorted(FINANCIAL_MUTATION_LINK_BOOKING_TYPES))
        raise MoneybirdError(f"booking_type must be one of: {supported}.")
    if not str(booking_id).strip():
        raise MoneybirdError("booking_id is required.")

    client = ctx.get_client()
    mutation = client.get_financial_mutation(financial_mutation_id)
    amount = parse_decimal_number(price, label="price") if str(price).strip() else None

    booking = clean_dict(
        {
            "booking_type": booking_type,
            "booking_id": str(booking_id).strip(),
            "price": str(amount) if amount is not None else "",
        }
    )
    return stage_write(
        "link_bank_mutation_booking",
        summary=f"Link financial mutation {financial_mutation_id} to {booking_type} {booking_id}",
        payload={
            "financial_mutation_id": str(financial_mutation_id),
            "booking": booking,
        },
        preview={
            "financial_mutation": {
                "id": str(mutation.get("id")),
                "date": mutation.get("date"),
                "message": mutation.get("message"),
                "contra_account_name": mutation.get("contra_account_name"),
                **_mutation_link_state(mutation),
            },
            "booking": booking,
            "booking_target": _link_target_summary(client, booking_type, str(booking_id)),
            "price_note": (
                "No price given: Moneybird links the full open amount."
                if amount is None
                else f"Explicit amount: {amount}."
            ),
        },
        fingerprint=duplicate_fingerprint(
            "link_bank_mutation_booking",
            {"financial_mutation_id": str(financial_mutation_id), "booking": booking},
        ),
    )


def _execute_link_booking(client, payload: dict[str, Any]) -> dict[str, Any]:
    mutation_id = payload["financial_mutation_id"]
    before = _mutation_link_state(client.get_financial_mutation(mutation_id))
    client.link_financial_mutation_booking(mutation_id, payload["booking"])
    after = _mutation_link_state(client.get_financial_mutation(mutation_id))
    link_visible = len(after["payments"]) > len(before["payments"]) or len(
        after["ledger_account_bookings"]
    ) > len(before["ledger_account_bookings"])
    return {
        "_status": "linked",
        "_audit": {
            "financial_mutation_id": str(mutation_id),
            "booking": payload["booking"],
        },
        "verification": {
            "before": before,
            "after": after,
            "new_link_visible_on_mutation": link_visible,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def link_bank_mutation_booking_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared bank mutation link."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "link_bank_mutation_booking", _execute_link_booking
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_unlink_bank_mutation_booking(
    financial_mutation_id: str,
    booking_type: str,
    booking_id: str,
) -> dict[str, Any]:
    """Use this before unlinking a wrongly matched booking from a bank/cash mutation.
    booking_type is Payment (a linked invoice/document payment) or LedgerAccountBooking
    (a direct category booking); booking_id is the id of that entry as shown on the mutation's
    payments / ledger_account_bookings. Do not execute the write until the user explicitly
    confirms."""
    booking_type = str(booking_type).strip()
    if booking_type not in FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES:
        supported = ", ".join(sorted(FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES))
        raise MoneybirdError(f"booking_type must be one of: {supported}.")

    client = ctx.get_client()
    mutation = client.get_financial_mutation(financial_mutation_id)
    state = _mutation_link_state(mutation)
    haystack = (
        state["payments"] if booking_type == "Payment" else state["ledger_account_bookings"]
    )
    target = next(
        (item for item in haystack if str(item.get("id")) == str(booking_id).strip()),
        None,
    )
    if target is None:
        raise MoneybirdError(
            f"No {booking_type} with id {booking_id} found on financial mutation "
            f"{financial_mutation_id}. Present: {haystack}."
        )

    # No fingerprint: unlinking the same booking again after a re-link is legitimate,
    # so duplicate suppression is intentionally disabled for this action.
    return stage_write(
        "unlink_bank_mutation_booking",
        summary=f"Unlink {booking_type} {booking_id} from financial mutation {financial_mutation_id}",
        payload={
            "financial_mutation_id": str(financial_mutation_id),
            "booking_type": booking_type,
            "booking_id": str(booking_id).strip(),
        },
        preview={
            "financial_mutation": {
                "id": str(mutation.get("id")),
                "date": mutation.get("date"),
                "message": mutation.get("message"),
                **state,
            },
            "unlink": target,
        },
    )


def _execute_unlink_booking(client, payload: dict[str, Any]) -> dict[str, Any]:
    mutation_id = payload["financial_mutation_id"]
    client.unlink_financial_mutation_booking(
        mutation_id,
        booking_type=payload["booking_type"],
        booking_id=payload["booking_id"],
    )
    after = _mutation_link_state(client.get_financial_mutation(mutation_id))
    haystack = (
        after["payments"]
        if payload["booking_type"] == "Payment"
        else after["ledger_account_bookings"]
    )
    still_present = any(
        str(item.get("id")) == payload["booking_id"] for item in haystack
    )
    return {
        "_status": "unlinked",
        "_audit": {
            "financial_mutation_id": str(mutation_id),
            "booking_type": payload["booking_type"],
            "booking_id": payload["booking_id"],
        },
        "verification": {
            "booking_removed_from_mutation": not still_present,
            "after": after,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def unlink_bank_mutation_booking_from_approval(approval_id: str) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared bank mutation unlink."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "unlink_bank_mutation_booking", _execute_unlink_booking
    )


