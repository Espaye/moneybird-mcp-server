"""Bank/cash mutations: reads plus guarded link/unlink of bookings (manual reconciliation)."""
from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from pydantic import Field

from ..config import (
    FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
    FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES,
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
    WRITE_ANNOTATIONS,
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
)
from ..invoicing import (
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


def _link_target_contract(
    client,
    booking_type: str,
    booking_id: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return a durable target snapshot and the human-facing preview."""

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
    return snapshot, preview


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


@mcp.tool(annotations=WRITE_ANNOTATIONS)
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
        Field(description="Optional partial amount as a decimal string, e.g. '121.00'. Empty = link the full open amount."),
    ] = "",
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
    target_snapshot, target_preview = _link_target_contract(
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
    amount = parse_decimal_number(price, label="price") if str(price).strip() else None
    if amount == 0:
        raise MoneybirdError("price must be non-zero when supplied.")

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
            "expected_mutation_version": mutation_version,
            "expected_mutation_state": _mutation_link_state(mutation),
            "expected_target": target_snapshot,
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
            "price_note": (
                "No price given: Moneybird links the full open amount."
                if amount is None
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
    current_target, _target_preview = _link_target_contract(
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
    expected_open = (
        Decimal("0.00")
        if not requested_price
        else money_decimal(before.get("amount_open"))
        - money_decimal(requested_price)
    )
    expected_price = (
        f"{money_decimal(requested_price):.2f}" if requested_price else ""
    )
    verification = {
        "mutation_id_matches": str(after_record.get("id") or "") == str(mutation_id),
        "exactly_one_new_link_visible": len(new_links) == 1,
        "new_link_visible_on_mutation": len(target_links) == 1,
        "booking_target_matches": len(target_links) == 1,
        "new_link_price_matches": (
            True
            if not requested_price
            else any(
                f"{money_decimal(item.get('price')):.2f}" == expected_price
                for item in target_links
            )
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
    return {
        "_status": "linked" if fully_verified else "completed_with_errors",
        "_audit_result": "success" if fully_verified else "verification_failed",
        "_audit": {
            "financial_mutation_id": str(mutation_id),
            "booking": payload["booking"],
            "fully_verified": fully_verified,
        },
        "verification": {
            "before": before,
            "after": after,
            **verification,
            "fully_verified": fully_verified,
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def link_bank_mutation_booking_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared bank mutation link."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "link_bank_mutation_booking", _execute_link_booking
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
            "unlink": target,
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
        },
    }


@mcp.tool(annotations=WRITE_ANNOTATIONS)
def unlink_bank_mutation_booking_from_approval(approval_id: ApprovalId) -> dict[str, Any]:
    """Use this only after the user has explicitly confirmed the prepared bank mutation unlink."""
    client = ctx.get_client()
    return run_approved_write(
        client, approval_id, "unlink_bank_mutation_booking", _execute_unlink_booking
    )


