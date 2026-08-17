"""Bank/cash mutations: reads plus guarded link/unlink of bookings (manual reconciliation)."""
from __future__ import annotations

from collections import Counter
from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from ..bank_matching import match_mutation, match_mutation_groups
from ..config import (
    FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
    FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES,
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    UNPAID_DOCUMENT_STATES,
    UNPAID_SALES_INVOICE_STATES,
    VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
    MoneybirdError,
)
from ..formatting import (
    clean_dict,
    compact_financial_mutation_summary,
    document_contact_title,
    duplicate_fingerprint,
    invoice_title,
    money_decimal,
    purchase_document_title,
    report_period_months,
    symbolic_period_months,
)
from ..invoicing import (
    details_attributes_payload,
    parse_decimal_number,
)
from ..task_context import MoneybirdTaskContext
from . import _context as ctx
from ._params import (
    ApprovalId,
    FilterString,
    FinancialMutationId,
    Limit,
    LinkBookingType,
    Page,
    Period,
    UnlinkBookingType,
)
from ._registry import mcp
from ._writes import (
    mark_write_dispatch_started,
    mark_write_verifying,
    run_approved_write,
    stage_write,
)
from .payments import _open_amount


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def list_financial_mutations(
    limit: Limit = 10,
    page: Page = 1,
    filter: FilterString = "",
    period: Period = "",
) -> dict[str, Any]:
    """List bankmutaties (bank or cash transactions, banktransacties, afschriftregels).
    Filter state:unprocessed for the onverwerkte transacties that still need booking.
    To find which invoice each one pays, use suggest_bank_mutation_matches."""
    client = ctx.get_client()
    mutations = client.list_financial_mutations(
        limit=limit,
        page=page,
        filter=filter,
        period=period,
    )
    financial_accounts = {
        str(item.get("id") or ""): item
        for item in client.list_financial_accounts(limit=100, page=1)
    }
    return {
        "financial_mutations": [
            compact_financial_mutation_summary(
                item,
                client.administration_id,
                financial_accounts.get(str(item.get("financial_account_id") or "")),
            )
            for item in mutations
        ],
        "page": page,
        "count": len(mutations),
    }


@mcp.tool(annotations=READ_ONLY_ANNOTATIONS)
def suggest_bank_mutation_matches(
    limit: Annotated[
        int,
        Field(
            ge=1,
            le=50,
            description="How many unprocessed mutations to analyse (1-50).",
        ),
    ] = 10,
    period: Period = "",
    financial_mutation_id: Annotated[
        str,
        Field(description="Optional single bank mutation id to analyse instead of a period scan."),
    ] = "",
) -> dict[str, Any]:
    """Welke factuur hoort bij deze bankmutatie? Match unprocessed bank transactions to
    the open invoices they pay: for each bankmutatie or banktransactie, find which open
    sales invoice, purchase invoice, or receipt it most likely settles, with the evidence.

    This is the read step of processing your bank feed — the same job Moneybird's
    own transaction screen does when it suggests a match. Money in is matched
    against open sales invoices, money out against open purchase invoices and
    receipts, using the invoice reference in the bank description, an exact open
    amount, the counterparty IBAN, and the contact name. Each candidate says which
    of those fired, so nothing is matched on a hunch.

    It changes nothing. Take a candidate you and the user agree on to
    prepare_link_bank_mutation_booking, which is where confirmation and
    verification happen. When suggestion is 'ambiguous' or 'none', ask the user
    rather than picking one.
    """
    client = ctx.get_client()

    if financial_mutation_id:
        mutations = [client.get_financial_mutation(financial_mutation_id)]
    else:
        mutations = client.list_financial_mutations(
            limit=limit,
            page=1,
            filter="state:unprocessed",
            period=period,
        )
    hidden = (
        []
        if financial_mutation_id
        else _unprocessed_hidden_by_state_filter(client, period, mutations)
    )
    if not mutations:
        return {
            "matches": [],
            "count": 0,
            "hidden_unprocessed": hidden,
            "note": (
                "No unprocessed bank mutations found for this period."
                + (
                    f" {len(hidden)} unprocessed mutation(s) are excluded by "
                    "Moneybird's own state filter because they never settled; "
                    "see hidden_unprocessed."
                    if hidden
                    else ""
                )
            ),
        }

    # Which sides actually need loading. An all-incoming batch never has to fetch
    # purchase invoices at all, which halves the cost of the common case.
    amounts = [_signed_amount(item) for item in mutations]
    needs_sales = any(amount is not None and amount > 0 for amount in amounts)
    needs_purchases = any(amount is not None and amount < 0 for amount in amounts)

    sales_invoices: list[dict[str, Any]] = []
    purchase_documents: list[tuple[str, dict[str, Any]]] = []
    warnings: list[str] = []
    if needs_sales:
        try:
            sales_invoices = client.list_sales_invoices(
                limit=100, page=1, state=UNPAID_SALES_INVOICE_STATES
            )
        except MoneybirdError as exc:
            warnings.append(f"open sales invoices could not be listed: {exc}")
    if needs_purchases:
        for kind in ("purchase_invoice", "receipt"):
            try:
                purchase_documents.extend(
                    (kind, document)
                    for document in client.list_documents(
                        kind,
                        limit=100,
                        page=1,
                        filter=f"state:{UNPAID_DOCUMENT_STATES}",
                    )
                )
            except MoneybirdError as exc:
                warnings.append(f"open {kind}s could not be listed: {exc}")

    matches = [
        match_mutation(
            mutation,
            sales_invoices=sales_invoices,
            purchase_documents=purchase_documents,
        )
        for mutation in mutations
    ]
    group_matches = match_mutation_groups(mutations, purchase_documents)
    summary = Counter(match["suggestion"] for match in matches)
    result: dict[str, Any] = {
        "matches": matches,
        "group_matches": group_matches,
        "count": len(matches),
        "summary": dict(summary),
        "candidate_pool": {
            "open_sales_invoices": len(sales_invoices),
            "open_purchase_documents": len(purchase_documents),
        },
        "next_step": (
            "Prefer one strong group_match when present: show its complete preview, "
            "then use prepare_settle_purchase_invoice_from_bank_mutations so one "
            "approval links every mutation and processes the invoice. Otherwise use "
            "prepare_link_bank_mutation_booking per agreed match. Mutations with "
            "suggestion 'none' usually belong on a ledger account rather than an invoice."
        ),
    }
    if not financial_mutation_id and _hidden_scan_skipped(period):
        result["hidden_unprocessed_note"] = (
            "Not checked for this period. Mutations that never settled are "
            "invisible to Moneybird's state filter, and finding them costs a "
            "request per month, so the check runs for a single month only. "
            "Re-run per month to see them."
        )
    if hidden:
        result["hidden_unprocessed"] = hidden
        result["next_step"] += (
            f" Separately, {len(hidden)} unprocessed mutation(s) are invisible to "
            "Moneybird's state filter because they never settled (a refused or "
            "reversed direct debit); see hidden_unprocessed. They usually need no "
            "booking, since no money moved, but nothing will ever surface them."
        )
    if warnings:
        result["warnings"] = warnings
    return result


def _hidden_scan_skipped(period: str) -> bool:
    """True when the period spans more than one month, so the advisory read is skipped."""
    months = report_period_months(period) or symbolic_period_months(period)
    return months is not None and len(months) > 1


def _unprocessed_hidden_by_state_filter(
    client,
    period: str,
    seen: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return unprocessed mutations that ``state:unprocessed`` does not return.

    Moneybird's state filter reports only mutations that settled, so a refused
    direct debit stays ``state: unprocessed`` forever while every scan built on
    that filter calls the feed empty. Re-reading the period without the filter
    is one extra request and is the only way to see them, so the backlog is
    reported rather than silently carried.

    Membership is decided on ``settlement_state``, never on absence from
    ``seen``: ``seen`` holds only the caller's page of matches, so a settled
    mutation sitting past that page would otherwise be reported as a failed
    collection that needs no booking — a far worse error than staying quiet.
    ``seen`` is used only to subtract rows already shown, which can remove a
    row from this list but never add one.

    Skipped for a period spanning more than one month. A wide period is served
    by splitting it into a request per month, so this advisory read would
    silently double the request count of an already expensive scan; the caller
    is told it was skipped rather than paying that without being asked.
    """
    if _hidden_scan_skipped(period):
        return []
    seen_ids = {str(item.get("id")) for item in seen}
    try:
        everything = client.list_financial_mutations(limit=100, page=1, period=period)
    except MoneybirdError:
        # Advisory only: never fail the match scan over the extra lookup.
        return []
    return [
        {
            "id": str(item.get("id") or ""),
            "date": str(item.get("date") or ""),
            "amount": str(item.get("amount") or ""),
            "contra_account_name": str(item.get("contra_account_name") or ""),
            "settlement_state": str(item.get("settlement_state") or ""),
        }
        for item in everything
        if str(item.get("state") or "") == "unprocessed"
        # An explicit non-settled state is the evidence; a blank one is unknown
        # rather than failed, so it is not claimed to be hidden.
        and str(item.get("settlement_state") or "") not in ("", "settled")
        and str(item.get("id")) not in seen_ids
    ]


def _signed_amount(mutation: dict[str, Any]) -> Decimal | None:
    for key in ("amount_open", "amount"):
        value = mutation.get(key)
        if value not in (None, ""):
            try:
                return money_decimal(value)
            except (ArithmeticError, ValueError, TypeError):
                continue
    return None


def _link_target_contract(
    client,
    booking_type: str,
    booking_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a durable target snapshot, the human-facing preview, and the record.

    The record is handed back so a caller can read fields that do not belong in
    the equality-compared snapshot — the document lines needed to book a still
    unbooked document through after its payment is linked.
    """

    if booking_type == "SalesInvoice":
        record = client.get_sales_invoice(booking_id)
        kind = ""
    elif booking_type == "LedgerAccount":
        record = client.get_ledger_account(booking_id)
        kind = ""
    elif booking_type == "Document":
        record = None
        kind = ""
        errors: list[str] = []
        for candidate in ("purchase_invoice", "receipt"):
            try:
                record = client.get_document(candidate, booking_id)
                kind = candidate
                break
            except MoneybirdError as exc:
                errors.append(str(exc))
        if record is None:
            raise MoneybirdError(
                f"Document {booking_id} was not found as a purchase invoice or "
                f"receipt. Lookups: {errors}."
            )
    else:
        raise MoneybirdError(
            f"booking_type {booking_type} has no exact post-write verifier."
        )

    record_id = str(record.get("id") or "")
    if record_id != str(booking_id):
        raise MoneybirdError(
            f"{booking_type} lookup for {booking_id} returned record {record_id or '<none>'}."
        )
    occurrence = str(record.get("version") or record.get("updated_at") or "")
    if booking_type == "LedgerAccount":
        snapshot = {
            "id": record_id,
            "occurrence": occurrence,
            "name": record.get("name"),
            "account_type": record.get("account_type"),
            "active": record.get("active"),
        }
        if record.get("active") is False:
            raise MoneybirdError(f"Ledger account {booking_id} is inactive.")
        preview = {
            "id": record_id,
            "title": record.get("name"),
            "account_type": record.get("account_type"),
            "active": record.get("active"),
        }
    else:
        snapshot = {
            "id": record_id,
            "occurrence": occurrence,
            "document_kind": kind,
            "state": record.get("state"),
            "currency": record.get("currency"),
            "total_price_incl_tax": record.get("total_price_incl_tax"),
            "open_amount": _open_amount(record),
        }
        preview = {
            "id": record_id,
            "title": (
                invoice_title(record)
                if booking_type == "SalesInvoice"
                else purchase_document_title(kind, record)
            ),
            "document_kind": kind or None,
            "contact": document_contact_title(record),
            "currency": record.get("currency"),
            "total_price_incl_tax": record.get("total_price_incl_tax"),
            "open_amount": _open_amount(record),
            "state": record.get("state"),
        }
    return snapshot, preview, record


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


def _resolve_bank_reclassification_target(
    ledger_accounts: list[dict[str, Any]],
    entry: dict[str, Any],
) -> dict[str, Any]:
    target_id = str(entry.get("target_ledger_account_id") or "").strip()
    target_name = str(entry.get("target_ledger_account_name") or "").strip()
    if target_id:
        target = next(
            (
                account
                for account in ledger_accounts
                if str(account.get("id") or "") == target_id
            ),
            None,
        )
        if target is None:
            raise MoneybirdError(f"Unknown target_ledger_account_id {target_id}.")
    elif target_name:
        matches = [
            account
            for account in ledger_accounts
            if str(account.get("name") or "") == target_name
        ]
        if len(matches) != 1:
            raise MoneybirdError(
                f"Expected exactly one ledger account named '{target_name}', "
                f"got {len(matches)}."
            )
        target = matches[0]
    else:
        raise MoneybirdError(
            "Each bank-booking reclassification needs "
            "target_ledger_account_id or target_ledger_account_name."
        )
    if target.get("active") is False:
        raise MoneybirdError(
            f"Target ledger account {target.get('name') or target.get('id')} is inactive."
        )
    return target


def _render_bank_reclassification_table(rows: list[dict[str, Any]]) -> str:
    headers = ["date", "mutation", "amount", "from", "to", "status"]
    values = [
        [
            str(row.get("date") or ""),
            str(row.get("financial_mutation_id") or ""),
            str(row.get("price") or ""),
            str(row.get("source_ledger_account") or ""),
            str(row.get("target_ledger_account") or ""),
            str(row.get("status") or ""),
        ]
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(row[index]) for row in values))
        for index in range(len(headers))
    ]
    return "\n".join(
        [
            " | ".join(
                headers[index].ljust(widths[index])
                for index in range(len(headers))
            ),
            "-+-".join("-" * widths[index] for index in range(len(headers))),
            *[
                " | ".join(
                    row[index].ljust(widths[index])
                    for index in range(len(headers))
                )
                for row in values
            ],
        ]
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_reclassify_bank_mutation_bookings(
    entries: Annotated[
        list[dict[str, Any]],
        Field(
            description=(
                "Direct bank-booking moves. Each dict requires "
                "financial_mutation_id, ledger_account_booking_id, and either "
                "target_ledger_account_id or target_ledger_account_name."
            )
        ),
    ],
) -> dict[str, Any]:
    """Prepare a guarded batch move of direct bank bookings between ledger accounts.

    The preview binds every move to the mutation version and exact source booking
    (id, ledger, amount and description). Execution preflights the complete batch,
    then unlinks and re-links each amount, verifies the mutation is still fully
    reconciled, and attempts to restore the source booking if a re-link fails.
    """
    if not entries:
        raise MoneybirdError("Provide at least one bank-booking reclassification.")
    if len(entries) > 100:
        raise MoneybirdError(
            "At most 100 bank-booking reclassifications can be prepared at once."
        )

    client = ctx.get_client()
    task = MoneybirdTaskContext(client)
    ledger_accounts = task.ledger_accounts()
    ledger_by_id = {
        str(account.get("id") or ""): account for account in ledger_accounts
    }
    prepared_items: list[dict[str, Any]] = []
    preview_rows: list[dict[str, Any]] = []
    seen_mutations: set[str] = set()
    seen_bookings: set[str] = set()
    normalized_entries: list[dict[str, Any]] = []

    for entry in entries:
        mutation_id = str(entry.get("financial_mutation_id") or "").strip()
        booking_id = str(entry.get("ledger_account_booking_id") or "").strip()
        if not mutation_id or not booking_id:
            raise MoneybirdError(
                "Each entry needs financial_mutation_id and "
                "ledger_account_booking_id."
            )
        if mutation_id in seen_mutations:
            raise MoneybirdError(
                f"Financial mutation {mutation_id} is listed more than once. "
                "Prepare one direct booking move per mutation."
            )
        if booking_id in seen_bookings:
            raise MoneybirdError(
                f"Ledger account booking {booking_id} is listed more than once."
            )
        seen_mutations.add(mutation_id)
        seen_bookings.add(booking_id)
        normalized_entries.append(
            {
                **entry,
                "financial_mutation_id": mutation_id,
                "ledger_account_booking_id": booking_id,
            }
        )

    mutations = task.financial_mutations(seen_mutations)

    for entry in normalized_entries:
        mutation_id = entry["financial_mutation_id"]
        booking_id = entry["ledger_account_booking_id"]
        mutation = mutations[mutation_id]
        source_booking = next(
            (
                booking
                for booking in mutation.get("ledger_account_bookings") or []
                if str(booking.get("id") or "") == booking_id
            ),
            None,
        )
        if source_booking is None:
            raise MoneybirdError(
                f"Ledger account booking {booking_id} is not present on "
                f"financial mutation {mutation_id}."
            )
        source_ledger_id = str(
            source_booking.get("ledger_account_id") or ""
        ).strip()
        source_ledger = ledger_by_id.get(source_ledger_id) or {
            "id": source_ledger_id,
            "name": source_ledger_id,
            "account_id": "",
        }
        target_ledger = _resolve_bank_reclassification_target(
            ledger_accounts, entry
        )
        target_ledger_id = str(target_ledger.get("id") or "")
        if source_ledger_id == target_ledger_id:
            raise MoneybirdError(
                f"Financial mutation {mutation_id} is already booked to "
                f"{target_ledger.get('name') or target_ledger_id}."
            )

        price = money_decimal(source_booking.get("price"))
        if price == Decimal("0.00"):
            raise MoneybirdError(
                f"Ledger account booking {booking_id} has a zero amount."
            )
        prepared_items.append(
            {
                "financial_mutation_id": mutation_id,
                "expected_version": str(mutation.get("version") or ""),
                "expected_state": mutation.get("state"),
                "expected_amount": f"{money_decimal(mutation.get('amount')):.2f}",
                "expected_amount_open": (
                    f"{money_decimal(mutation.get('amount_open')):.2f}"
                ),
                "source_booking": {
                    "id": booking_id,
                    "ledger_account_id": source_ledger_id,
                    "price": f"{price:.2f}",
                    "description": source_booking.get("description"),
                },
                "target_ledger_account_id": target_ledger_id,
                "target_ledger_account_name": target_ledger.get("name"),
            }
        )
        source_label = " ".join(
            part
            for part in [
                str(source_ledger.get("account_id") or ""),
                str(source_ledger.get("name") or ""),
            ]
            if part
        )
        target_label = " ".join(
            part
            for part in [
                str(target_ledger.get("account_id") or ""),
                str(target_ledger.get("name") or ""),
            ]
            if part
        )
        preview_rows.append(
            {
                "financial_mutation_id": mutation_id,
                "ledger_account_booking_id": booking_id,
                "date": mutation.get("date"),
                "contra_account_name": mutation.get("contra_account_name"),
                "payment_reference": (
                    (mutation.get("sepa_fields") or {}).get("remi")
                    or (mutation.get("sepa_fields") or {}).get("strd_remi")
                    or mutation.get("message")
                ),
                "price": f"{price:.2f}",
                "source_ledger_account_id": source_ledger_id,
                "source_ledger_account": source_label,
                "target_ledger_account_id": target_ledger_id,
                "target_ledger_account": target_label,
                "status": "ready",
            }
        )

    payload = {"items": prepared_items}
    fingerprint = duplicate_fingerprint(
        "reclassify_bank_mutation_bookings", payload
    )
    return stage_write(
        "reclassify_bank_mutation_bookings",
        summary=(
            f"Reclassify {len(prepared_items)} direct bank mutation booking(s)"
        ),
        payload=payload,
        preview={
            "preview_table": _render_bank_reclassification_table(preview_rows),
            "rows": preview_rows,
            "mutation_count": len(prepared_items),
            "total_absolute_amount": (
                f"{sum((abs(money_decimal(row['price'])) for row in preview_rows), Decimal('0')):.2f}"
            ),
            "safety": {
                "full_batch_preflight_before_writes": True,
                "source_restore_attempt_on_failed_relink": True,
                "post_write_verification": (
                    "source booking removed, new target booking visible, "
                    "amount/state/open amount unchanged"
                ),
                "api_transaction_available": False,
            },
        },
        fingerprint=fingerprint,
    )


def _preflight_bank_reclassification(
    client: Any,
    items: list[dict[str, Any]],
    *,
    task: MoneybirdTaskContext | None = None,
) -> dict[str, dict[str, Any]]:
    task = task or MoneybirdTaskContext(client)
    ledger_accounts = {
        str(account.get("id") or ""): account
        for account in task.ledger_accounts(refresh=True)
    }
    mutations = task.financial_mutations(
        [item["financial_mutation_id"] for item in items],
        refresh=True,
    )
    for item in items:
        mutation_id = item["financial_mutation_id"]
        mutation = mutations[mutation_id]
        current_version = str(mutation.get("version") or "")
        if item.get("expected_version") and (
            current_version != item["expected_version"]
        ):
            raise MoneybirdError(
                f"Financial mutation {mutation_id} changed after the preview "
                f"(version {item['expected_version']} -> {current_version}). "
                "Prepare the batch again."
            )
        source = item["source_booking"]
        booking = next(
            (
                candidate
                for candidate in mutation.get("ledger_account_bookings") or []
                if str(candidate.get("id") or "") == source["id"]
            ),
            None,
        )
        if booking is None:
            raise MoneybirdError(
                f"Source booking {source['id']} disappeared from financial "
                f"mutation {mutation_id}. Prepare the batch again."
            )
        current_signature = (
            str(booking.get("ledger_account_id") or ""),
            f"{money_decimal(booking.get('price')):.2f}",
            booking.get("description"),
        )
        expected_signature = (
            source["ledger_account_id"],
            source["price"],
            source.get("description"),
        )
        if current_signature != expected_signature:
            raise MoneybirdError(
                f"Source booking {source['id']} on financial mutation "
                f"{mutation_id} changed after the preview. Prepare the batch again."
            )
        target = ledger_accounts.get(item["target_ledger_account_id"])
        if target is None or target.get("active") is False:
            raise MoneybirdError(
                f"Target ledger account {item['target_ledger_account_id']} is "
                "missing or inactive. Prepare the batch again."
            )
    return mutations


def _matching_new_target_booking(
    mutation: dict[str, Any],
    *,
    target_ledger_account_id: str,
    price: str,
    prior_booking_ids: set[str],
) -> dict[str, Any] | None:
    return next(
        (
            booking
            for booking in mutation.get("ledger_account_bookings") or []
            if str(booking.get("id") or "") not in prior_booking_ids
            and str(booking.get("ledger_account_id") or "")
            == target_ledger_account_id
            and f"{money_decimal(booking.get('price')):.2f}" == price
        ),
        None,
    )


def _verify_bank_reclassification_result(
    mutation: dict[str, Any],
    item: dict[str, Any],
    *,
    prior_booking_ids: set[str],
) -> tuple[dict[str, Any] | None, dict[str, bool]]:
    source = item["source_booking"]
    new_target = _matching_new_target_booking(
        mutation,
        target_ledger_account_id=item["target_ledger_account_id"],
        price=source["price"],
        prior_booking_ids=prior_booking_ids,
    )
    verification = {
        "source_booking_removed": not any(
            str(booking.get("id") or "") == source["id"]
            for booking in mutation.get("ledger_account_bookings") or []
        ),
        "new_target_booking_visible": new_target is not None,
        "amount_unchanged": (
            f"{money_decimal(mutation.get('amount')):.2f}"
            == item["expected_amount"]
        ),
        "amount_open_unchanged": (
            f"{money_decimal(mutation.get('amount_open')):.2f}"
            == item["expected_amount_open"]
        ),
        "state_unchanged": mutation.get("state") == item["expected_state"],
    }
    return new_target, verification


def _restore_source_bank_booking(
    client: Any,
    item: dict[str, Any],
    *,
    prior_booking_ids: set[str],
) -> dict[str, Any]:
    mutation_id = item["financial_mutation_id"]
    source = item["source_booking"]
    current = client.get_financial_mutation(mutation_id)
    new_target = _matching_new_target_booking(
        current,
        target_ledger_account_id=item["target_ledger_account_id"],
        price=source["price"],
        prior_booking_ids=prior_booking_ids,
    )
    if new_target is not None:
        client.unlink_financial_mutation_booking(
            mutation_id,
            booking_type="LedgerAccountBooking",
            booking_id=str(new_target.get("id")),
        )
        current = client.get_financial_mutation(mutation_id)

    def is_restored_source(booking: dict[str, Any]) -> bool:
        booking_id = str(booking.get("id") or "")
        return (
            str(booking.get("ledger_account_id") or "")
            == source["ledger_account_id"]
            and f"{money_decimal(booking.get('price')):.2f}" == source["price"]
            and (
                booking_id == source["id"]
                or booking_id not in prior_booking_ids
            )
        )

    source_present = any(
        is_restored_source(booking)
        for booking in current.get("ledger_account_bookings") or []
    )
    if not source_present:
        client.link_financial_mutation_booking(
            mutation_id,
            {
                "booking_type": "LedgerAccount",
                "booking_id": source["ledger_account_id"],
                "price": source["price"],
            },
        )
    restored = client.get_financial_mutation(mutation_id)
    source_restored = any(
        is_restored_source(booking)
        for booking in restored.get("ledger_account_bookings") or []
    )
    target_absent = not any(
        str(booking.get("ledger_account_id") or "")
        == item["target_ledger_account_id"]
        and f"{money_decimal(booking.get('price')):.2f}" == source["price"]
        and str(booking.get("id") or "") not in prior_booking_ids
        for booking in restored.get("ledger_account_bookings") or []
    )
    return {
        "attempted": True,
        "source_restored": source_restored,
        "new_target_removed": target_absent,
        "amount_open_restored": (
            f"{money_decimal(restored.get('amount_open')):.2f}"
            == item["expected_amount_open"]
        ),
        "state_restored": restored.get("state") == item["expected_state"],
        "version_after_restore": restored.get("version"),
    }


def _execute_reclassify_bank_mutation_bookings(
    client: Any,
    payload: dict[str, Any],
) -> dict[str, Any]:
    items = payload["items"]
    task = MoneybirdTaskContext(client)
    preflight = _preflight_bank_reclassification(client, items, task=task)
    completed: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    verification_context: dict[str, set[str]] = {}
    item_by_mutation = {
        item["financial_mutation_id"]: item for item in items
    }

    mark_write_dispatch_started()
    for item in items:
        mutation_id = item["financial_mutation_id"]
        source = item["source_booking"]
        before = preflight[mutation_id]
        prior_booking_ids = {
            str(booking.get("id") or "")
            for booking in before.get("ledger_account_bookings") or []
        }
        verification_context[mutation_id] = prior_booking_ids
        try:
            unlink_result = client.unlink_financial_mutation_booking(
                mutation_id,
                booking_type="LedgerAccountBooking",
                booking_id=source["id"],
            )
            after_unlink = (
                unlink_result
                if isinstance(unlink_result, dict)
                else client.get_financial_mutation(mutation_id)
            )
            if any(
                str(booking.get("id") or "") == source["id"]
                for booking in after_unlink.get("ledger_account_bookings") or []
            ):
                raise MoneybirdError(
                    f"Source booking {source['id']} is still visible after unlink."
                )

            link_result = client.link_financial_mutation_booking(
                mutation_id,
                {
                    "booking_type": "LedgerAccount",
                    "booking_id": item["target_ledger_account_id"],
                    "price": source["price"],
                },
            )
            after = (
                link_result
                if isinstance(link_result, dict)
                else client.get_financial_mutation(mutation_id)
            )
            new_target, verification = _verify_bank_reclassification_result(
                after,
                item,
                prior_booking_ids=prior_booking_ids,
            )
            if not all(verification.values()):
                raise MoneybirdError(
                    f"Post-write verification failed for financial mutation "
                    f"{mutation_id}: {verification}."
                )
            completed.append(
                {
                    "financial_mutation_id": mutation_id,
                    "source_booking_id": source["id"],
                    "source_ledger_account_id": source["ledger_account_id"],
                    "target_ledger_account_id": item[
                        "target_ledger_account_id"
                    ],
                    "target_booking_id": str(new_target.get("id")),
                    "price": source["price"],
                    "version_before": item.get("expected_version") or None,
                    "version_after": after.get("version"),
                    "verification": verification,
                }
            )
        except Exception as exc:
            try:
                restore = _restore_source_bank_booking(
                    client,
                    item,
                    prior_booking_ids=prior_booking_ids,
                )
            except Exception as restore_exc:
                restore = {
                    "attempted": True,
                    "source_restored": False,
                    "error": str(restore_exc),
                }
            failures.append(
                {
                    "financial_mutation_id": mutation_id,
                    "source_booking_id": source["id"],
                    "error": str(exc),
                    "restore": restore,
                }
            )
            break

    mark_write_verifying()
    # The endpoint responses above provide immediate, per-step verification.
    # Re-fetch every completed mutation independently in one synchronization
    # batch before reporting success.
    if completed:
        final_records = task.financial_mutations(
            [row["financial_mutation_id"] for row in completed],
            refresh=True,
        )
        final_failures: list[dict[str, Any]] = []
        for row in list(completed):
            mutation_id = row["financial_mutation_id"]
            item = item_by_mutation[mutation_id]
            new_target, final_verification = _verify_bank_reclassification_result(
                final_records[mutation_id],
                item,
                prior_booking_ids=verification_context[mutation_id],
            )
            if not all(final_verification.values()):
                completed.remove(row)
                final_failures.append(
                    {
                        "financial_mutation_id": mutation_id,
                        "source_booking_id": item["source_booking"]["id"],
                        "error": (
                            "Independent batch verification failed after the "
                            f"write: {final_verification}."
                        ),
                        "restore": {
                            "attempted": False,
                            "reason": (
                                "The immediate API response was valid but the "
                                "independent re-fetch differs; automatic restore "
                                "would risk overwriting a concurrent change."
                            ),
                        },
                    }
                )
                continue
            row["target_booking_id"] = str(new_target.get("id"))
            row["version_after"] = final_records[mutation_id].get("version")
            row["verification"] = final_verification
            row["verification_source"] = "independent_batch_refetch"
        failures.extend(final_failures)

    fully_verified = not failures and len(completed) == len(items)
    return {
        "_status": (
            "completed"
            if fully_verified
            else "completed_with_errors"
        ),
        "_audit_result": "success" if fully_verified else "partial_failure",
        "_audit": {
            "completed_count": len(completed),
            "failure_count": len(failures),
            "fully_verified": fully_verified,
        },
        "fully_verified": fully_verified,
        "completed": completed,
        "failures": failures,
        "not_started_count": max(
            0,
            len(items) - len(completed) - len(failures),
        ),
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def reclassify_bank_mutation_bookings_from_approval(
    approval_id: ApprovalId,
) -> dict[str, Any]:
    """Execute one prepared direct-bank-booking batch after explicit approval."""
    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "reclassify_bank_mutation_bookings",
        _execute_reclassify_bank_mutation_bookings,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_link_bank_mutation_booking(
    financial_mutation_id: FinancialMutationId,
    booking_type: LinkBookingType,
    booking_id: Annotated[
        str,
        Field(description="Id of the record to link, matching booking_type (e.g. the sales invoice, document, or ledger account id)."),
    ],
    price: Annotated[
        str,
        Field(description="Optional partial amount as a decimal string, e.g. '121.00'. Empty = use the mutation's current amount_open."),
    ] = "",
) -> dict[str, Any]:
    """Book a bank transaction to a ledger, invoice, or purchase document. Dutch: bankmutatie koppelen aan factuur or bankmutatie boeken op grootboek.

    Use this before linking a bank/cash mutation to a booking: an open invoice or document
    (booking_type SalesInvoice or Document) or directly to a ledger category (LedgerAccount).
    This is the manual counterpart of Moneybird's bank reconciliation. When price is empty, the
    preview explicitly fills it from the mutation's current amount_open; Moneybird never receives
    a nil price.

    A Document still in state 'new' is not booked by a payment link alone, so once the
    link verifies the document is saved unchanged to book it through to 'paid' — unless
    the payment leaves an open balance, in which case it stays 'new' on purpose. The
    result reports the resulting state under 'document'. Do not execute the write until
    the user explicitly confirms."""
    booking_type = str(booking_type).strip()
    if booking_type not in FINANCIAL_MUTATION_LINK_BOOKING_TYPES:
        supported = ", ".join(sorted(FINANCIAL_MUTATION_LINK_BOOKING_TYPES))
        raise MoneybirdError(f"booking_type must be one of: {supported}.")
    if booking_type not in VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES:
        supported = ", ".join(
            sorted(VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES)
        )
        raise MoneybirdError(
            f"The guarded link tool supports only exactly verifiable booking types: "
            f"{supported}."
        )
    if not str(booking_id).strip():
        raise MoneybirdError("booking_id is required.")

    client = ctx.get_client()
    mutation = client.get_financial_mutation(financial_mutation_id)
    target_snapshot, target_preview, target_record = _link_target_contract(
        client,
        booking_type,
        str(booking_id).strip(),
    )
    mutation_version = str(
        mutation.get("version") or mutation.get("updated_at") or ""
    )
    if not mutation_version:
        raise MoneybirdError(
            "Moneybird did not return a mutation version/updated_at value; "
            "cannot bind this repeatable link safely."
        )
    price_was_defaulted = not str(price).strip()
    price_source = mutation.get("amount_open") if price_was_defaulted else price
    try:
        amount = parse_decimal_number(price_source, label="price")
    except MoneybirdError as exc:
        if not price_was_defaulted:
            raise
        raise MoneybirdError(
            "price was omitted, but Moneybird did not return a usable "
            "amount_open for this financial mutation. Pass price explicitly."
        ) from exc
    if amount == 0:
        if price_was_defaulted:
            raise MoneybirdError(
                "price was omitted, but the financial mutation's amount_open is zero. "
                "There is no open amount to link."
            )
        raise MoneybirdError("price must be non-zero when supplied.")

    booking = clean_dict(
        {
            "booking_type": booking_type,
            "booking_id": str(booking_id).strip(),
            "price": str(amount),
        }
    )
    warnings: list[str] = []
    target_account_type = str(target_snapshot.get("account_type") or "")
    if booking_type == "LedgerAccount" and target_account_type in {
        "revenue",
        "expenses",
        "direct_costs",
        "other_income_expenses",
    }:
        warnings.append(
            "A direct ledger booking does not accept tax_rate_id and creates no VAT "
            "posting: the full amount is booked to this profit-and-loss account. If "
            "the amount includes VAT, link it to an invoice/document or use an "
            "explicit balanced journal with separate VAT lines. For a VAT-exempt "
            "cost this is the correct booking, not a compromise: bank charges, "
            "interest and other financial services carry no input VAT "
            "(see get_bookkeeping_guide('btw'))."
        )
    # Linking a payment settles the money but does not book an unbooked document:
    # it stays in state 'new' and keeps surfacing in review_purchase_invoices as
    # "not booked yet". The grouped settlement tool already finishes the job, so
    # the single-mutation path does too rather than leaving a half-processed
    # document behind for a difference no caller can see.
    book_through = None
    if booking_type == "Document" and str(target_record.get("state") or "") == "new":
        book_through = {
            "document_kind": str(target_snapshot.get("document_kind") or ""),
            "document_id": str(booking_id).strip(),
            "prices_are_incl_tax": bool(target_record.get("prices_are_incl_tax")),
            "lines": _processing_details(target_record.get("details") or []),
        }
        warnings.append(
            "This document is still in state 'new' (not booked yet). Linking the "
            "payment alone would settle it while leaving it unbooked, so after the "
            "link is verified the document is saved unchanged to book it through "
            "to 'paid'. No ledger account, tax rate, description or amount is "
            "altered, and the booking is skipped if the payment does not close "
            "the document's full open amount."
        )

    return stage_write(
        "link_bank_mutation_booking",
        summary=f"Link financial mutation {financial_mutation_id} to {booking_type} {booking_id}",
        payload={
            "financial_mutation_id": str(financial_mutation_id),
            "booking": booking,
            "expected_mutation_version": mutation_version,
            "expected_mutation_state": _mutation_link_state(mutation),
            "expected_target": target_snapshot,
            "book_through": book_through,
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
            "booking_target": target_preview,
            "warnings": warnings,
            "price_note": (
                f"Price defaulted to the mutation's amount_open: {amount}."
                if price_was_defaulted
                else f"Explicit amount: {amount}."
            ),
        },
        fingerprint=duplicate_fingerprint(
            "link_bank_mutation_booking",
            {
                "financial_mutation_id": str(financial_mutation_id),
                "booking": booking,
                "mutation_occurrence": mutation_version,
                "target_occurrence": target_snapshot,
            },
        ),
    )


def _execute_link_booking(client, payload: dict[str, Any]) -> dict[str, Any]:
    mutation_id = payload["financial_mutation_id"]
    before_record = client.get_financial_mutation(mutation_id)
    if str(before_record.get("id") or "") != str(mutation_id):
        raise MoneybirdError(
            f"Financial mutation {mutation_id} lookup returned a different record. "
            "Prepare again."
        )
    current_version = str(
        before_record.get("version") or before_record.get("updated_at") or ""
    )
    if current_version != str(payload.get("expected_mutation_version") or ""):
        raise MoneybirdError(
            f"Financial mutation {mutation_id} changed after preview. Prepare again."
        )
    before = _mutation_link_state(before_record)
    if before != payload.get("expected_mutation_state"):
        raise MoneybirdError(
            f"Financial mutation {mutation_id} booking state changed after preview. "
            "Prepare again."
        )
    current_target, _target_preview, _target_record = _link_target_contract(
        client,
        str(payload["booking"]["booking_type"]),
        str(payload["booking"]["booking_id"]),
    )
    if current_target != payload.get("expected_target"):
        raise MoneybirdError(
            f"The {payload['booking']['booking_type']} target changed after preview. "
            "Prepare again."
        )
    mark_write_dispatch_started()
    client.link_financial_mutation_booking(mutation_id, payload["booking"])
    mark_write_verifying()
    after_record = client.get_financial_mutation(mutation_id)
    after = _mutation_link_state(after_record)
    before_ids = {
        str(item.get("id") or "")
        for item in before["payments"] + before["ledger_account_bookings"]
    }
    new_payment_links = [
        item
        for item in after["payments"]
        if str(item.get("id") or "") not in before_ids
    ]
    new_ledger_links = [
        item
        for item in after["ledger_account_bookings"]
        if str(item.get("id") or "") not in before_ids
    ]
    new_links = new_payment_links + new_ledger_links
    requested_type = str(payload["booking"]["booking_type"])
    requested_id = str(payload["booking"]["booking_id"])
    target_links = (
        [
            item
            for item in new_ledger_links
            if str(item.get("ledger_account_id") or "") == requested_id
        ]
        if requested_type == "LedgerAccount"
        else [
            item
            for item in new_payment_links
            if str(item.get("invoice_id") or "") == requested_id
            and str(item.get("invoice_type") or "") == requested_type
        ]
    )
    requested_price = str(payload["booking"].get("price") or "").strip()
    expected_open = money_decimal(before.get("amount_open")) - money_decimal(
        requested_price
    )
    # Moneybird's link endpoint accepts the signed bank-mutation amount, but a
    # payment stored on an invoice/document is returned as a positive magnitude.
    # Ledger bookings retain the mutation sign.
    expected_price = f"{money_decimal(requested_price):.2f}"
    expected_link_price = (
        expected_price
        if requested_type == "LedgerAccount"
        else f"{abs(money_decimal(requested_price)):.2f}"
    )
    verification = {
        "mutation_id_matches": str(after_record.get("id") or "") == str(mutation_id),
        "exactly_one_new_link_visible": len(new_links) == 1,
        "new_link_visible_on_mutation": len(target_links) == 1,
        "booking_target_matches": len(target_links) == 1,
        "new_link_price_matches": any(
            f"{money_decimal(item.get('price')):.2f}" == expected_link_price
            for item in target_links
        ),
        "amount_open_matches": (
            f"{money_decimal(after.get('amount_open')):.2f}"
            == f"{expected_open:.2f}"
        ),
        "closed_mutation_is_processed": (
            expected_open != Decimal("0.00") or after.get("state") == "processed"
        ),
    }
    fully_verified = all(verification.values())
    write_effect_observed = after != before
    document = _book_document_through(client, payload, link_verified=fully_verified)
    if document is not None:
        verification["document_booked_out_of_new"] = bool(
            document.get("state_after") == "paid"
        )
        fully_verified = all(verification.values())
    status = (
        "linked" if fully_verified else "completed_with_errors" if write_effect_observed else "failed"
    )
    result = {
        "_status": status,
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "financial_mutation_id": str(mutation_id),
            "booking": payload["booking"],
            "fully_verified": fully_verified,
        },
        "verification": {
            "before": before,
            "after": after,
            "write_effect_observed": write_effect_observed,
            **verification,
            "fully_verified": fully_verified,
        },
    }
    if document is not None:
        result["document"] = document
    return result


def _book_document_through(
    client,
    payload: dict[str, Any],
    *,
    link_verified: bool,
) -> dict[str, Any] | None:
    """Save a linked-but-unbooked document so Moneybird books it through to 'paid'.

    A payment link settles the money; it does not book a document that is still
    in state 'new'. Re-saving it with its own current lines is the same
    status-triggering save the grouped settlement path performs, and changes no
    ledger account, tax rate, description or amount.

    Returns None when there was nothing to book. The save is skipped unless the
    link itself verified and the document's open amount actually reached zero,
    because booking a partially paid document through would assert a settlement
    that did not happen.
    """
    book_through = payload.get("book_through")
    if not book_through:
        return None
    kind = str(book_through.get("document_kind") or "purchase_invoice")
    document_id = str(book_through.get("document_id") or "")
    outcome: dict[str, Any] = {
        "document_kind": kind,
        "document_id": document_id,
        "state_before": "new",
        "attempted": False,
    }
    if not link_verified:
        outcome["skipped_reason"] = (
            "The payment link did not verify, so the document was left untouched."
        )
        return outcome
    try:
        before_save = client.get_document(kind, document_id)
        open_amount = money_decimal(_open_amount(before_save))
        if str(before_save.get("state") or "") == "paid":
            outcome["state_after"] = "paid"
            outcome["skipped_reason"] = "Moneybird already booked it through."
            return outcome
        if open_amount != Decimal("0.00"):
            outcome["state_after"] = str(before_save.get("state") or "")
            outcome["open_amount"] = f"{open_amount:.2f}"
            outcome["skipped_reason"] = (
                "The payment does not close the document's full open amount, so it "
                "stays in 'new' until the remainder is settled."
            )
            return outcome
        outcome["attempted"] = True
        total_before = str(before_save.get("total_price_incl_tax") or "")
        client.update_document(
            kind,
            document_id,
            {
                "prices_are_incl_tax": book_through["prices_are_incl_tax"],
                "details_attributes": details_attributes_payload(
                    book_through["lines"]
                ),
            },
        )
        after_save = client.get_document(kind, document_id)
        total_after = str(after_save.get("total_price_incl_tax") or "")
        outcome["state_after"] = str(after_save.get("state") or "")
        outcome["total_before"] = total_before
        outcome["total_after"] = total_after
        outcome["total_unchanged"] = (
            money_decimal(total_before) == money_decimal(total_after)
        )
    except Exception as exc:  # noqa: BLE001 - the payment link is already durable
        # The money is settled either way; report the gap rather than failing the
        # whole action and implying the link did not happen.
        outcome["error"] = str(exc)
        outcome["state_after"] = "new"
    return outcome


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def link_bank_mutation_booking_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared bank mutation link."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "link_bank_mutation_booking", _execute_link_booking
    )


def _purchase_invoice_lines(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Stable booking fields that must survive the status-triggering save."""

    return sorted(
        (
            {
                "id": str(line.get("id") or ""),
                "description": str(line.get("description") or ""),
                "price": f"{money_decimal(line.get('price')):.2f}",
                "amount": str(line.get("amount_decimal") or line.get("amount") or "1"),
                "ledger_account_id": str(line.get("ledger_account_id") or ""),
                "tax_rate_id": str(line.get("tax_rate_id") or ""),
                "project_id": str(line.get("project_id") or ""),
                "product_id": str(line.get("product_id") or ""),
                "period": str(line.get("period") or ""),
                "row_order": int(line.get("row_order") or 0),
            }
            for line in (record.get("details") or [])
        ),
        key=lambda line: (line["row_order"], line["id"]),
    )


def _purchase_invoice_settlement_snapshot(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(record.get("id") or ""),
        "version": str(record.get("version") or record.get("updated_at") or ""),
        "state": record.get("state"),
        "total_price_incl_tax": f"{money_decimal(record.get('total_price_incl_tax')):.2f}",
        "open_amount": f"{money_decimal(_open_amount(record)):.2f}",
        "prices_are_incl_tax": bool(record.get("prices_are_incl_tax")),
        "lines": _purchase_invoice_lines(record),
    }


def _processing_details(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "id": str(detail.get("id") or ""),
            "description": str(detail.get("description") or ""),
            "price": f"{money_decimal(detail.get('price')):.2f}",
            "amount": str(detail.get("amount") or "1"),
        }
        for detail in lines
    ]


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_settle_purchase_invoice_from_bank_mutations(
    document_id: Annotated[
        str,
        Field(description="Internal Moneybird id of the purchase invoice to settle."),
    ],
    financial_mutation_ids: Annotated[
        list[str],
        Field(
            min_length=2,
            max_length=10,
            description=(
                "Two to ten unprocessed outgoing bank mutation ids whose absolute "
                "amounts exactly equal the invoice's current open amount."
            ),
        ),
    ],
) -> dict[str, Any]:
    """Prepare one approval to link several bankmutaties and process one inkoopfactuur.

    Only an unambiguous supplier group that exactly closes the open balance is accepted.
    The executor preflights every record and verifies the paid invoice after the writes.
    """
    document_id = str(document_id or "").strip()
    mutation_ids = [str(item or "").strip() for item in financial_mutation_ids]
    if not document_id:
        raise MoneybirdError("document_id is required.")
    if any(not item for item in mutation_ids) or len(set(mutation_ids)) != len(
        mutation_ids
    ):
        raise MoneybirdError("financial_mutation_ids must be non-empty and unique.")
    if not 2 <= len(mutation_ids) <= 10:
        raise MoneybirdError("Provide between 2 and 10 unique financial mutation ids.")

    client = ctx.get_client()
    task = MoneybirdTaskContext(client)
    invoice = task.documents("purchase_invoice", [document_id])[document_id]
    mutations = task.financial_mutations(mutation_ids)
    expected_invoice = _purchase_invoice_settlement_snapshot(invoice)
    if not expected_invoice["version"]:
        raise MoneybirdError("The purchase invoice has no version/updated_at value.")
    open_amount = money_decimal(expected_invoice["open_amount"])
    if expected_invoice["state"] == "paid" or open_amount <= 0:
        raise MoneybirdError(f"Purchase invoice {document_id} has no open amount.")
    if not expected_invoice["lines"]:
        raise MoneybirdError(f"Purchase invoice {document_id} has no booking lines.")
    if any(
        not line["id"] or not line["ledger_account_id"] or not line["tax_rate_id"]
        for line in expected_invoice["lines"]
    ):
        raise MoneybirdError(
            "The purchase invoice has incomplete ledger/tax lines."
        )

    items: list[dict[str, Any]] = []
    for mutation_id in mutation_ids:
        mutation = mutations[mutation_id]
        state = _mutation_link_state(mutation)
        version = str(mutation.get("version") or mutation.get("updated_at") or "")
        if not version:
            raise MoneybirdError(f"Financial mutation {mutation_id} has no version.")
        if state["state"] != "unprocessed" or state["payments"] or state["ledger_account_bookings"]:
            raise MoneybirdError(f"Financial mutation {mutation_id} is already booked.")
        amount = money_decimal(state["amount_open"])
        if amount >= 0:
            raise MoneybirdError(f"Financial mutation {mutation_id} is not outgoing.")
        items.append(
            {
                "financial_mutation_id": mutation_id,
                "expected_version": version,
                "expected_state": state,
                "price": f"{amount:.2f}",
                "preview": {
                    "date": mutation.get("date"),
                    "contra_account_name": mutation.get("contra_account_name"),
                    "amount": f"{amount:.2f}",
                },
            }
        )
    total = sum((abs(money_decimal(item["price"])) for item in items), Decimal())
    if total != open_amount:
        raise MoneybirdError(
            f"Selected mutations total {total:.2f}; invoice {document_id} has "
            f"{open_amount:.2f} open and must be closed exactly."
        )
    groups = match_mutation_groups(
        [mutations[mutation_id] for mutation_id in mutation_ids],
        [("purchase_invoice", invoice)],
    )
    if not any(
        group["suggestion"] == "strong"
        and set(group["financial_mutation_ids"]) == set(mutation_ids)
        for group in groups
    ):
        raise MoneybirdError(
            "The selected mutations do not form one unambiguous supplier group."
        )

    payload = {
        "document_id": document_id,
        "expected_invoice": expected_invoice,
        "items": items,
    }
    return stage_write(
        "settle_purchase_invoice_from_bank_mutations",
        summary=(
            f"Link {len(items)} bank mutations totalling {total:.2f} to purchase "
            f"invoice {invoice.get('reference') or document_id} and process it"
        ),
        payload=payload,
        preview={
            "purchase_invoice": {
                "id": document_id,
                "reference": invoice.get("reference"),
                "contact": document_contact_title(invoice),
                "date": invoice.get("date"),
                "state_before": invoice.get("state"),
                "state_after_expected": "paid",
                "total_price_incl_tax": expected_invoice["total_price_incl_tax"],
                "open_amount": expected_invoice["open_amount"],
                "lines_unchanged": True,
            },
            "mutations": [item["preview"] for item in items],
            "mutation_count": len(items),
            "total": f"{total:.2f}",
        },
        fingerprint=duplicate_fingerprint(
            "settle_purchase_invoice_from_bank_mutations", payload
        ),
    )


def _group_payment_verified(
    mutation: dict[str, Any],
    *,
    document_id: str,
    price: str,
) -> bool:
    expected_payment = f"{abs(money_decimal(price)):.2f}"
    matches = [
        payment
        for payment in (mutation.get("payments") or [])
        if str(payment.get("invoice_id") or "") == document_id
        and str(payment.get("invoice_type") or "") == "Document"
        and f"{money_decimal(payment.get('price')):.2f}" == expected_payment
    ]
    return (
        mutation.get("state") == "processed"
        and money_decimal(mutation.get("amount_open")) == Decimal("0.00")
        and len(matches) == 1
    )


def _execute_group_settlement(client, payload: dict[str, Any]) -> dict[str, Any]:
    document_id = payload["document_id"]
    mutation_ids = [item["financial_mutation_id"] for item in payload["items"]]
    task = MoneybirdTaskContext(client)
    invoice = task.documents(
        "purchase_invoice", [document_id], refresh=True
    )[document_id]
    mutations = task.financial_mutations(mutation_ids, refresh=True)
    if _purchase_invoice_settlement_snapshot(invoice) != payload["expected_invoice"]:
        raise MoneybirdError(
            "The purchase invoice changed after the grouped settlement preview. "
            "Prepare it again."
        )
    for item in payload["items"]:
        mutation = mutations[item["financial_mutation_id"]]
        version = str(mutation.get("version") or mutation.get("updated_at") or "")
        if (
            version != item["expected_version"]
            or _mutation_link_state(mutation) != item["expected_state"]
        ):
            raise MoneybirdError(
                f"Financial mutation {item['financial_mutation_id']} changed after "
                "the grouped settlement preview. Prepare it again."
            )

    completed: list[str] = []
    failures: list[dict[str, Any]] = []
    mark_write_dispatch_started()
    for item in payload["items"]:
        mutation_id = item["financial_mutation_id"]
        try:
            client.link_financial_mutation_booking(
                mutation_id,
                {
                    "booking_type": "Document",
                    "booking_id": document_id,
                    "price": item["price"],
                },
            )
            completed.append(mutation_id)
        except Exception as exc:  # noqa: BLE001 - preserve a partial batch outcome
            failures.append(
                {
                    "step": "link_bank_mutation",
                    "financial_mutation_id": mutation_id,
                    "error": str(exc),
                }
            )
            break

    linked_mutations = task.financial_mutations(mutation_ids, refresh=True)
    mutation_verification = {
        item["financial_mutation_id"]: _group_payment_verified(
                linked_mutations[item["financial_mutation_id"]],
                document_id=document_id,
                price=item["price"],
            )
        for item in payload["items"]
    }
    if not failures and not all(mutation_verification.values()):
        failures.append({"step": "verify_bank_mutations", "error": "Not all links persisted."})

    if not failures and len(completed) == len(payload["items"]):
        try:
            invoice_after_links = client.get_document("purchase_invoice", document_id)
            if invoice_after_links.get("state") != "paid":
                client.update_document(
                    "purchase_invoice",
                    document_id,
                    {
                        "prices_are_incl_tax": payload["expected_invoice"][
                            "prices_are_incl_tax"
                        ],
                        "details_attributes": details_attributes_payload(
                            _processing_details(payload["expected_invoice"]["lines"])
                        ),
                    },
                )
        except Exception as exc:  # noqa: BLE001 - links may already be durable
            failures.append(
                {
                    "step": "process_purchase_invoice",
                    "document_id": document_id,
                    "error": str(exc),
                }
            )

    mark_write_verifying()
    final_invoice = task.documents(
        "purchase_invoice", [document_id], refresh=True
    )[document_id]

    expected_invoice = payload["expected_invoice"]
    invoice_verification = {
        "state_paid": final_invoice.get("state") == "paid",
        "paid_at_visible": bool(final_invoice.get("paid_at")),
        "open_amount_zero": money_decimal(_open_amount(final_invoice))
        == Decimal("0.00"),
        "total_unchanged": (
            f"{money_decimal(final_invoice.get('total_price_incl_tax')):.2f}"
            == expected_invoice["total_price_incl_tax"]
        ),
        "lines_unchanged": _purchase_invoice_lines(final_invoice)
        == expected_invoice["lines"],
    }
    fully_verified = (
        not failures
        and len(completed) == len(payload["items"])
        and all(mutation_verification.values())
        and all(invoice_verification.values())
    )
    return {
        "_status": "completed" if fully_verified else "completed_with_errors",
        "_audit_result": "success" if fully_verified else "partial_failure",
        "_audit": {
            "document_id": document_id,
            "completed_link_count": len(completed),
            "failure_count": len(failures),
            "fully_verified": fully_verified,
        },
        "fully_verified": fully_verified,
        "completed": completed,
        "failures": failures,
        "verification": {
            "purchase_invoice": {
                "id": document_id,
                "reference": final_invoice.get("reference"),
                "state": final_invoice.get("state"),
                "paid_at": final_invoice.get("paid_at"),
                **invoice_verification,
            },
            "mutations": mutation_verification,
        },
    }


def settle_purchase_invoice_from_bank_mutations_from_approval(
    approval_id: ApprovalId,
) -> dict[str, Any]:
    """Execute one explicitly approved grouped purchase-invoice settlement."""

    client = ctx.get_client()
    return run_approved_write(
        client,
        approval_id,
        "settle_purchase_invoice_from_bank_mutations",
        _execute_group_settlement,
    )


@mcp.tool(annotations=PREPARE_ANNOTATIONS)
def prepare_unlink_bank_mutation_booking(
    financial_mutation_id: FinancialMutationId,
    booking_type: UnlinkBookingType,
    booking_id: Annotated[
        str,
        Field(description="Id of the payment or ledger-account-booking entry as shown on the mutation."),
    ],
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
    mutation_version = str(
        mutation.get("version") or mutation.get("updated_at") or ""
    )
    if not mutation_version:
        raise MoneybirdError(
            "Moneybird did not return a mutation version/updated_at value; "
            "cannot bind this repeatable unlink safely."
        )
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
    unlink_preview = dict(target)
    if booking_type == "LedgerAccountBooking":
        ledger_account_id = str(target.get("ledger_account_id") or "")
        if ledger_account_id:
            ledger_account = client.get_ledger_account(ledger_account_id)
            unlink_preview["ledger_account_name"] = ledger_account.get("name")
            unlink_preview["ledger_account_number"] = ledger_account.get("account_id")

    return stage_write(
        "unlink_bank_mutation_booking",
        summary=f"Unlink {booking_type} {booking_id} from financial mutation {financial_mutation_id}",
        payload={
            "financial_mutation_id": str(financial_mutation_id),
            "booking_type": booking_type,
            "booking_id": str(booking_id).strip(),
            "expected_mutation_version": mutation_version,
            "expected_booking": target,
        },
        preview={
            "financial_mutation": {
                "id": str(mutation.get("id")),
                "date": mutation.get("date"),
                "message": mutation.get("message"),
                **state,
            },
            "unlink": unlink_preview,
        },
        fingerprint=duplicate_fingerprint(
            "unlink_bank_mutation_booking",
            {
                "financial_mutation_id": str(financial_mutation_id),
                "booking_type": booking_type,
                "booking_id": str(booking_id).strip(),
                "mutation_occurrence": mutation_version,
            },
        ),
    )


def _execute_unlink_booking(client, payload: dict[str, Any]) -> dict[str, Any]:
    mutation_id = payload["financial_mutation_id"]
    before_record = client.get_financial_mutation(mutation_id)
    if str(before_record.get("id") or "") != str(mutation_id):
        raise MoneybirdError(
            f"Financial mutation {mutation_id} lookup returned a different record. "
            "Prepare again."
        )
    current_version = str(
        before_record.get("version") or before_record.get("updated_at") or ""
    )
    if current_version != str(payload.get("expected_mutation_version") or ""):
        raise MoneybirdError(
            f"Financial mutation {mutation_id} changed after preview. Prepare again."
        )
    before_state = _mutation_link_state(before_record)
    before_haystack = (
        before_state["payments"]
        if payload["booking_type"] == "Payment"
        else before_state["ledger_account_bookings"]
    )
    current_booking = next(
        (
            item
            for item in before_haystack
            if str(item.get("id")) == payload["booking_id"]
        ),
        None,
    )
    if current_booking != payload.get("expected_booking"):
        raise MoneybirdError(
            f"Booking {payload['booking_id']} changed after preview. Prepare again."
        )
    mark_write_dispatch_started()
    client.unlink_financial_mutation_booking(
        mutation_id,
        booking_type=payload["booking_type"],
        booking_id=payload["booking_id"],
    )
    mark_write_verifying()
    after_record = client.get_financial_mutation(mutation_id)
    after = _mutation_link_state(after_record)
    haystack = (
        after["payments"]
        if payload["booking_type"] == "Payment"
        else after["ledger_account_bookings"]
    )
    still_present = any(
        str(item.get("id")) == payload["booking_id"] for item in haystack
    )
    record_id_matches = str(after_record.get("id") or "") == str(mutation_id)
    fully_verified = record_id_matches and not still_present
    return {
        "_status": (
            "unlinked" if fully_verified else "completed_with_verification_errors"
        ),
        "_audit_result": (
            "success" if fully_verified else "verification_failed"
        ),
        "_audit": {
            "financial_mutation_id": str(mutation_id),
            "booking_type": payload["booking_type"],
            "booking_id": payload["booking_id"],
            "fully_verified": fully_verified,
        },
        "verification": {
            "record_id_matches": record_id_matches,
            "booking_removed_from_mutation": not still_present,
            "after": after,
            "fully_verified": fully_verified,
        },
    }


# Not registered as an MCP tool: every approved action executes through the single
# annotated execute_approved_action entry point. Kept as a Python function because
# tools/approvals.py dispatches to it and scripts/tests call it directly.
def unlink_bank_mutation_booking_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared bank mutation unlink."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "unlink_bank_mutation_booking", _execute_unlink_booking
    )


