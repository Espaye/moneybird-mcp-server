"""Bookkeeping business logic: journal validation, invoice/merge/reclassification flows."""
from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from .client import MoneybirdClient
from .config import (
    DOCUMENT_POSTABLE_ACCOUNT_TYPES,
    MERGE_FIELD_LABELS,
    MoneybirdError,
)
from .formatting import (
    api_url,
    chunked,
    clean_dict,
    contact_delivery_record,
    contact_invoice_email,
    contact_title,
    document_line_quantity,
    duplicate_fingerprint,
    matches_query,
    money_decimal,
    normalize_document_kind,
    normalize_text,
    normalized_text,
    purchase_document_title,
    render_preview_table,
    year_period_for_date,
)
from .write_contracts import build_patch_precondition


def parse_decimal_number(value: Any, *, label: str) -> Decimal:
    text = str(value).strip().replace("\u00a0", " ")
    # Accept one explicit decimal separator and nothing else. In particular,
    # never reinterpret a currency suffix, grouping separator, or second dot by
    # silently taking a numeric prefix.
    match = re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?", text)
    if not match:
        raise MoneybirdError(f"Invalid {label}: {value!r}.")
    try:
        result = Decimal(match.group(0).replace(",", "."))
    except InvalidOperation as exc:
        raise MoneybirdError(f"Invalid {label}: {value!r}.") from exc
    if not result.is_finite():
        raise MoneybirdError(f"Invalid {label}: {value!r}.")
    return result

def validate_general_journal_entries(entries: list[dict[str, Any]]) -> dict[str, Any]:
    if len(entries) < 2:
        raise MoneybirdError("Provide at least two journal entries.")
    prepared_entries: list[dict[str, Any]] = []
    total_debit = Decimal("0")
    total_credit = Decimal("0")
    for entry in entries:
        if not entry.get("ledger_account_id"):
            raise MoneybirdError("Each journal entry needs ledger_account_id.")
        debit = money_decimal(entry.get("debit", 0))
        credit = money_decimal(entry.get("credit", 0))
        if debit < 0 or credit < 0:
            raise MoneybirdError("Journal entry debit/credit values must be non-negative.")
        if debit == 0 and credit == 0:
            raise MoneybirdError("Journal entries cannot have both debit and credit equal to zero.")
        if debit and credit:
            raise MoneybirdError("A journal entry cannot contain both debit and credit.")
        total_debit += debit
        total_credit += credit
        prepared_entries.append(
            clean_dict(
                {
                    "ledger_account_id": str(entry.get("ledger_account_id")),
                    "tax_rate_id": entry.get("tax_rate_id"),
                    "description": entry.get("description", ""),
                    "contact_id": entry.get("contact_id"),
                    "project_id": entry.get("project_id"),
                    "debit": str(debit),
                    "credit": str(credit),
                }
            )
        )
    if total_debit != total_credit:
        raise MoneybirdError(
            f"General journal is not balanced: debit {total_debit} vs credit {total_credit}."
        )
    return {
        "entries": prepared_entries,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
    }




def resolve_ledger_account_reference(
    client: MoneybirdClient,
    *,
    ledger_account_id: str = "",
    ledger_account_name: str = "",
) -> dict[str, Any]:
    ledger_accounts = client.list_ledger_accounts()
    if ledger_account_id:
        match = next(
            (item for item in ledger_accounts if str(item.get("id")) == str(ledger_account_id)),
            None,
        )
        if not match:
            raise MoneybirdError(f"Unknown ledger_account_id {ledger_account_id}.")
        return match
    if ledger_account_name:
        matches = [
            item for item in ledger_accounts if str(item.get("name") or "") == ledger_account_name
        ]
        if len(matches) != 1:
            raise MoneybirdError(
                f"Expected exactly one ledger account named '{ledger_account_name}', got {len(matches)}."
            )
        return matches[0]
    raise MoneybirdError("Provide ledger_account_id or ledger_account_name.")




def validate_document_ledger_target(kind: str, ledger_account: dict[str, Any]) -> None:
    normalized_kind = normalize_document_kind(kind)
    if normalized_kind not in {"purchase_invoice", "receipt"}:
        return
    account_type = str(ledger_account.get("account_type") or "")
    if account_type not in DOCUMENT_POSTABLE_ACCOUNT_TYPES:
        raise MoneybirdError(
            "Purchase invoices and receipts can only be reclassified directly to "
            "profit-and-loss ledger accounts. Use a balancing general journal for "
            "asset or liability moves."
        )




def document_detail_amount_excl_tax(detail: dict[str, Any]) -> Decimal:
    candidates = [
        detail.get("total_price_excl_tax"),
        detail.get("price_excl_tax"),
        detail.get("price"),
    ]
    for candidate in candidates:
        if candidate not in (None, ""):
            amount = money_decimal(candidate)
            if candidate != detail.get("price"):
                return amount
            # Only a unit price has to be scaled by the line quantity, and that
            # field is messy in older data ("1 x", ""); see document_line_quantity.
            quantity = document_line_quantity(detail.get("amount"))
            if quantity == Decimal("1"):
                return amount
            return (amount * quantity).quantize(
                Decimal("0.01"),
                rounding=ROUND_HALF_UP,
            )
    return Decimal("0.00")




def resolve_document_detail(
    document: dict[str, Any],
    *,
    detail_id: str = "",
    row_order: int | None = None,
) -> dict[str, Any]:
    details = document.get("details") or []
    if detail_id:
        match = next(
            (detail for detail in details if str(detail.get("id")) == str(detail_id)),
            None,
        )
        if not match:
            raise MoneybirdError(
                f"Could not find detail_id {detail_id} on document {document.get('id')}."
            )
        return match
    if row_order is not None:
        match = next(
            (
                detail
                for detail in details
                if int(detail.get("row_order", 0)) == int(row_order)
            ),
            None,
        )
        if not match:
            raise MoneybirdError(
                f"Could not find row_order {row_order} on document {document.get('id')}."
            )
        return match
    raise MoneybirdError("Provide detail_id or row_order for each document line update.")




def details_attributes_payload(details: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(index): detail for index, detail in enumerate(details)}




def fetch_all_contacts(client: MoneybirdClient) -> list[dict[str, Any]]:
    versions = client.list_contact_versions()
    contacts: list[dict[str, Any]] = []
    ids = [str(item["id"]) for item in versions if item.get("id")]
    for id_batch in chunked(ids, 100):
        contacts.extend(client.fetch_contacts_by_ids(id_batch))
    return contacts




def fetch_all_recurring_sales_invoices(client: MoneybirdClient) -> list[dict[str, Any]]:
    versions = client.list_recurring_sales_invoice_versions()
    recurring_sales_invoices: list[dict[str, Any]] = []
    ids = [str(item["id"]) for item in versions if item.get("id")]
    for id_batch in chunked(ids, 100):
        recurring_sales_invoices.extend(client.fetch_recurring_sales_invoices_by_ids(id_batch))
    return recurring_sales_invoices




def recurring_sales_invoice_delivery_issue(
    recurring_sales_invoice: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any] | None:
    contact = recurring_sales_invoice.get("contact") or {}
    delivery_method = str(contact.get("delivery_method") or "")
    invoice_email = contact_invoice_email(contact)
    reasons: list[str] = []

    if recurring_sales_invoice.get("auto_send") is not True:
        reasons.append(f"auto_send={recurring_sales_invoice.get('auto_send')}")
    if delivery_method != "Email":
        reasons.append(f"contact_delivery_method={delivery_method or 'empty'}")
    if not invoice_email:
        reasons.append("missing_invoice_email")

    if not reasons:
        return None

    recurring_id = str(recurring_sales_invoice.get("id") or "")
    return {
        "recurring_sales_invoice_id": recurring_id,
        "contact": contact_delivery_record(contact, administration_id),
        "auto_send": recurring_sales_invoice.get("auto_send"),
        "active": recurring_sales_invoice.get("active"),
        "next_invoice_date": recurring_sales_invoice.get("invoice_date"),
        "frequency_type": recurring_sales_invoice.get("frequency_type"),
        "frequency": recurring_sales_invoice.get("frequency"),
        "reasons": reasons,
        "url": api_url(
            "recurring_sales_invoices",
            recurring_id,
            administration_id,
        )
        if recurring_id
        else None,
    }




def original_sales_invoice_send_events(invoice: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for event in invoice.get("events") or []:
        action = str(event.get("action") or "")
        if not action.startswith("sales_invoice_send"):
            continue
        if "reminder" in action or "late_fee" in action:
            continue
        events.append(event)
    return events




def latest_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not events:
        return None
    return max(events, key=lambda event: str(event.get("created_at") or ""))




def classify_sales_invoice_send(
    invoice: dict[str, Any],
    administration_id: str | None = None,
) -> dict[str, Any]:
    send_event = latest_event(original_sales_invoice_send_events(invoice))
    action = str((send_event or {}).get("action") or "")
    all_actions = {str(event.get("action") or "") for event in invoice.get("events") or []}
    scheduled = "sales_invoice_state_changed_to_scheduled" in all_actions
    recurring = bool(invoice.get("recurring_sales_invoice_id")) or (
        "sales_invoice_created_based_on_recurring" in all_actions
    )
    contact = invoice.get("contact") or {}

    if action == "sales_invoice_send_manually":
        classification = "manual"
    elif action == "sales_invoice_send_email" and scheduled:
        classification = "automatic_email"
    elif action == "sales_invoice_send_email":
        classification = "manual_email"
    elif action == "sales_invoice_send_si_delivered" and scheduled:
        classification = "automatic_si"
    elif action == "sales_invoice_send_si_delivered":
        classification = "si_delivered"
    elif action:
        classification = "unknown"
    else:
        classification = "no_send_event"

    invoice_id = str(invoice.get("id") or "")
    return {
        "sales_invoice_id": invoice_id,
        "invoice_id": invoice.get("invoice_id"),
        "invoice_date": invoice.get("invoice_date"),
        "sent_at": invoice.get("sent_at"),
        "send_event_action": action,
        "send_event_at": (send_event or {}).get("created_at"),
        "classification": classification,
        "scheduled": scheduled,
        "recurring": recurring,
        "contact": contact_delivery_record(
            contact,
            administration_id or invoice.get("administration_id"),
        ),
    }




def build_recent_sales_invoice_send_method_audit(
    client: MoneybirdClient,
    *,
    limit: int = 30,
    page_scan_limit: int = 10,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    safe_limit = max(1, min(limit, 100))
    safe_page_scan_limit = max(1, min(page_scan_limit, 25))

    for page in range(1, safe_page_scan_limit + 1):
        invoices = client.list_sales_invoices(limit=100, page=page, state="all")
        if not invoices:
            break
        for invoice in invoices:
            if not original_sales_invoice_send_events(invoice):
                continue
            record = classify_sales_invoice_send(invoice, client.administration_id)
            record["url"] = api_url(
                "sales_invoices",
                record["sales_invoice_id"],
                client.administration_id,
            )
            rows.append(record)
        if len(rows) >= safe_limit:
            break

    rows.sort(key=lambda item: str(item.get("send_event_at") or ""), reverse=True)
    rows = rows[:safe_limit]
    counts: dict[str, int] = {}
    for row in rows:
        classification = str(row.get("classification") or "unknown")
        counts[classification] = counts.get(classification, 0) + 1

    return {
        "summary": {
            "requested_limit": limit,
            "returned_count": len(rows),
            "page_scan_limit": safe_page_scan_limit,
            "counts": counts,
        },
        "sales_invoices": rows,
    }




def build_invoice_delivery_audit(
    client: MoneybirdClient,
    *,
    include_archived_contacts: bool = False,
    include_inactive_recurring: bool = False,
) -> dict[str, Any]:
    contacts = fetch_all_contacts(client)
    checked_contacts = [
        contact
        for contact in contacts
        if include_archived_contacts or not contact.get("archived")
    ]
    non_email_contacts = [
        contact_delivery_record(contact, client.administration_id)
        for contact in checked_contacts
        if str(contact.get("delivery_method") or "") != "Email"
    ]
    email_without_invoice_email = [
        contact_delivery_record(contact, client.administration_id)
        for contact in checked_contacts
        if str(contact.get("delivery_method") or "") == "Email"
        and not contact_invoice_email(contact)
    ]

    recurring_sales_invoices = fetch_all_recurring_sales_invoices(client)
    checked_recurring = [
        recurring_sales_invoice
        for recurring_sales_invoice in recurring_sales_invoices
        if include_inactive_recurring or recurring_sales_invoice.get("active")
    ]
    recurring_issues = [
        issue
        for issue in (
            recurring_sales_invoice_delivery_issue(
                recurring_sales_invoice,
                client.administration_id,
            )
            for recurring_sales_invoice in checked_recurring
        )
        if issue is not None
    ]

    non_email_contacts.sort(
        key=lambda item: (str(item.get("delivery_method")), str(item.get("title")))
    )
    email_without_invoice_email.sort(key=lambda item: str(item.get("title")))
    recurring_issues.sort(
        key=lambda item: (
            str(item.get("contact", {}).get("title")),
            str(item.get("recurring_sales_invoice_id")),
        )
    )

    return {
        "summary": {
            "contacts_total": len(contacts),
            "contacts_checked": len(checked_contacts),
            "non_email_contact_count": len(non_email_contacts),
            "email_without_invoice_email_count": len(email_without_invoice_email),
            "recurring_sales_invoices_total": len(recurring_sales_invoices),
            "recurring_sales_invoices_checked": len(checked_recurring),
            "recurring_issue_count": len(recurring_issues),
        },
        "non_email_contacts": non_email_contacts,
        "email_without_invoice_email": email_without_invoice_email,
        "recurring_issues": recurring_issues,
        "include_archived_contacts": include_archived_contacts,
        "include_inactive_recurring": include_inactive_recurring,
    }




def resolve_contact_reference(
    client: MoneybirdClient,
    *,
    contact_id: str = "",
    customer_id: str = "",
) -> dict[str, Any]:
    by_id = getattr(client, "_moneybird_contacts_by_id", {})
    by_customer_id = getattr(client, "_moneybird_contacts_by_customer_id", {})
    if contact_id:
        contact = by_id.get(str(contact_id)) or client.get_contact(contact_id)
    elif customer_id:
        contact = by_customer_id.get(str(customer_id)) or client.get_contact_by_customer_id(customer_id)
    else:
        raise MoneybirdError("Provide contact_id or customer_id.")
    contact_id_value = str(contact.get("id") or "")
    customer_id_value = str(contact.get("customer_id") or "")
    if contact_id_value:
        by_id[contact_id_value] = contact
    if customer_id_value:
        by_customer_id[customer_id_value] = contact
    setattr(client, "_moneybird_contacts_by_id", by_id)
    setattr(client, "_moneybird_contacts_by_customer_id", by_customer_id)
    return contact




def list_contact_invoices_cached(
    client: MoneybirdClient,
    *,
    contact_id: str,
    period: str,
) -> list[dict[str, Any]]:
    cache = getattr(client, "_moneybird_invoices_by_contact_period", {})
    key = (str(contact_id), str(period))
    if key not in cache:
        cache[key] = client.list_sales_invoices(
            limit=100,
            page=1,
            state="all",
            contact_id=contact_id,
            period=period,
        )
        setattr(client, "_moneybird_invoices_by_contact_period", cache)
    return cache[key]


def get_latest_invoice_for_contact(client: MoneybirdClient, contact_id: str) -> dict[str, Any] | None:
    cache = getattr(client, "_moneybird_latest_invoice_by_contact", {})
    if contact_id in cache:
        return cache[contact_id]
    history_cache = getattr(client, "_moneybird_invoices_by_contact_period", {})
    cached_invoices = [
        invoice
        for (cached_contact_id, _), invoices in history_cache.items()
        if cached_contact_id == str(contact_id)
        for invoice in invoices
    ]
    if cached_invoices:
        cache[contact_id] = max(
            cached_invoices,
            key=lambda invoice: str(
                invoice.get("invoice_date") or invoice.get("created_at") or ""
            ),
        )
        setattr(client, "_moneybird_latest_invoice_by_contact", cache)
        return cache[contact_id]
    for period in ("this_year", "prev_year"):
        invoices = list_contact_invoices_cached(
            client,
            contact_id=contact_id,
            period=period,
        )
        if invoices:
            cache[contact_id] = max(
                invoices,
                key=lambda invoice: str(
                    invoice.get("invoice_date") or invoice.get("created_at") or ""
                ),
            )
            setattr(client, "_moneybird_latest_invoice_by_contact", cache)
            return cache[contact_id]
    cache[contact_id] = None
    setattr(client, "_moneybird_latest_invoice_by_contact", cache)
    return None


def find_latest_matching_invoice_detail(
    client: MoneybirdClient,
    *,
    contact_id: str,
    meter: str,
    description_prefix: str,
    primary_period: str = "this_year",
) -> dict[str, Any] | None:
    """Find the newest prior invoice line for the same metered unit."""
    meter_pattern = re.compile(
        rf"\b{re.escape(description_prefix.casefold())}\s+{re.escape(meter.casefold())}\b"
    )
    matches: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    periods = [primary_period]
    if primary_period != "prev_year":
        periods.append("prev_year")
    for period in periods:
        invoices = list_contact_invoices_cached(
            client,
            contact_id=contact_id,
            period=period,
        )
        for invoice in invoices:
            for detail in invoice.get("details") or []:
                description = str(detail.get("description") or "")
                if meter_pattern.search(description.casefold()):
                    matches.append(
                        (
                            str(invoice.get("invoice_date") or invoice.get("created_at") or ""),
                            invoice,
                            detail,
                        )
                    )
        if matches:
            break
    if not matches:
        return None
    _, invoice, detail = max(matches, key=lambda item: item[0])
    return {
        "invoice": invoice,
        "detail": detail,
    }


def format_decimal_nl(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}".replace(".", ",")


def build_meter_usage_entries(
    client: MoneybirdClient,
    *,
    rows: list[dict[str, Any]],
    period_label: str,
    invoice_date: str,
    schedule_send_on: str = "",
    minimum_usage_kwh: Any = "0",
    description_prefix: str = "Elektra",
    default_unit_price: str = "",
    default_tax_rate_id: str = "",
    default_ledger_account_id: str = "",
    skip_meters: list[str] | None = None,
) -> dict[str, Any]:
    if not rows:
        raise MoneybirdError("Provide at least one meter row.")
    if not period_label.strip():
        raise MoneybirdError("period_label is required (for example 2026-K2).")
    if not invoice_date.strip():
        raise MoneybirdError("invoice_date is required.")

    threshold = parse_decimal_number(minimum_usage_kwh, label="minimum_usage_kwh")
    if threshold < 0:
        raise MoneybirdError("minimum_usage_kwh must be non-negative.")
    skip_set = {str(item).strip().casefold() for item in (skip_meters or [])}
    entries: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    seen_meters: set[str] = set()

    for row in rows:
        meter = str(row.get("meter") or row.get("customer_id") or "").strip()
        if not meter:
            raise MoneybirdError("Each meter row needs meter or customer_id.")
        meter_key = meter.casefold()
        if meter_key in seen_meters:
            raise MoneybirdError(f"Meter {meter} is listed more than once.")
        seen_meters.add(meter_key)

        explicit_action = str(row.get("action") or "").strip().casefold()
        if explicit_action not in {"", "skip", "draft", "schedule", "merge", "separate"}:
            raise MoneybirdError(
                f"Unsupported action '{explicit_action}' for meter {meter}."
            )

        begin = row.get("begin_reading")
        end = row.get("end_reading")
        supplied_usage = row.get("usage_kwh")
        if supplied_usage not in (None, ""):
            usage = parse_decimal_number(supplied_usage, label=f"usage_kwh for {meter}")
            if begin not in (None, "") and end not in (None, ""):
                calculated = parse_decimal_number(end, label=f"end_reading for {meter}") - parse_decimal_number(
                    begin, label=f"begin_reading for {meter}"
                )
                if calculated.quantize(Decimal("0.01")) != usage.quantize(Decimal("0.01")):
                    raise MoneybirdError(
                        f"usage_kwh for {meter} does not equal end_reading - begin_reading."
                    )
        elif begin not in (None, "") and end not in (None, ""):
            usage = parse_decimal_number(end, label=f"end_reading for {meter}") - parse_decimal_number(
                begin, label=f"begin_reading for {meter}"
            )
        else:
            raise MoneybirdError(
                f"Meter {meter} needs usage_kwh or both begin_reading and end_reading."
            )
        usage = usage.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        if usage < 0:
            raise MoneybirdError(f"Meter {meter} has negative usage ({usage}).")

        skip_reason = ""
        if explicit_action == "skip" or meter_key in skip_set:
            skip_reason = "explicitly_skipped"
        elif usage < threshold:
            skip_reason = f"below_minimum_{threshold}"
        if skip_reason:
            decisions.append(
                {
                    "meter": meter,
                    "customer_id": row.get("customer_id") or meter,
                    "usage_kwh": f"{usage:.2f}",
                    "action": "skip",
                    "reason": skip_reason,
                }
            )
            continue

        customer_id = str(row.get("customer_id") or meter).strip()
        contact = resolve_contact_reference(client, customer_id=customer_id)
        source = find_latest_matching_invoice_detail(
            client,
            contact_id=str(contact["id"]),
            meter=meter,
            description_prefix=description_prefix,
            primary_period=year_period_for_date(invoice_date),
        )
        source_invoice = (source or {}).get("invoice") or {}
        source_detail = (source or {}).get("detail") or {}
        unit_price = str(row.get("unit_price") or default_unit_price or source_detail.get("price") or "")
        tax_rate_id = str(
            row.get("tax_rate_id")
            or default_tax_rate_id
            or source_detail.get("tax_rate_id")
            or ""
        )
        ledger_account_id = str(
            row.get("ledger_account_id")
            or default_ledger_account_id
            or source_detail.get("ledger_account_id")
            or ""
        )
        if not unit_price or not tax_rate_id or not ledger_account_id:
            raise MoneybirdError(
                f"Could not resolve unit price, tax rate and ledger account for meter {meter}. "
                "Provide explicit defaults or ensure a previous matching invoice exists."
            )

        action = explicit_action or ("schedule" if schedule_send_on else "draft")
        should_schedule = action in {"schedule", "merge", "separate"}
        row_send_on = str(row.get("schedule_send_on") or schedule_send_on or "").strip()
        if should_schedule and not row_send_on:
            raise MoneybirdError(f"Meter {meter} is scheduled but no schedule_send_on was provided.")
        prices_are_incl_tax = bool(
            row.get(
                "prices_are_incl_tax",
                source_invoice.get("prices_are_incl_tax", False),
            )
        )
        reference = str(
            row.get("reference")
            or f"STROOM-{period_label.strip()}-{meter.upper()}"
        )
        entry = {
            "contact_id": str(contact["id"]),
            "customer_id": customer_id,
            "reference": reference,
            "invoice_date": invoice_date,
            "prices_are_incl_tax": prices_are_incl_tax,
            "details": [
                {
                    "description": f"{description_prefix} {meter} - {format_decimal_nl(usage)} kWh",
                    "amount": f"{usage:.2f}",
                    "price": unit_price,
                    "tax_rate_id": tax_rate_id,
                    "ledger_account_id": ledger_account_id,
                    "period": row.get("period", ""),
                }
            ],
        }
        if should_schedule:
            entry["schedule_send_on"] = row_send_on
        entries.append(entry)
        decisions.append(
            {
                "meter": meter,
                "customer_id": customer_id,
                "contact_id": str(contact["id"]),
                "usage_kwh": f"{usage:.2f}",
                "unit_price": unit_price,
                "tax_rate_id": tax_rate_id,
                "ledger_account_id": ledger_account_id,
                "action": "schedule" if should_schedule else "draft",
                "merge_intent": action if action in {"merge", "separate"} else "unspecified",
                "schedule_send_on": row_send_on if should_schedule else "",
                "reference": reference,
                "source_invoice_id": str(source_invoice.get("id") or ""),
                "source_invoice_number": source_invoice.get("invoice_id"),
                "source_invoice_date": source_invoice.get("invoice_date"),
            }
        )

    if not entries:
        raise MoneybirdError("All meter rows were skipped; there are no invoices to prepare.")
    return {"entries": entries, "decisions": decisions}




def infer_contact_invoice_defaults(client: MoneybirdClient, contact: dict[str, Any]) -> dict[str, Any]:
    latest_invoice = get_latest_invoice_for_contact(client, str(contact["id"]))
    details = (latest_invoice or {}).get("details") or [{}]
    first_detail = details[0]
    return {
        "workflow_id": (latest_invoice or {}).get("workflow_id")
        or contact.get("invoice_workflow_id"),
        "document_style_id": (latest_invoice or {}).get("document_style_id"),
        "identity_id": (latest_invoice or {}).get("identity_id"),
        "language": (latest_invoice or {}).get("language") or "nl",
        "currency": (latest_invoice or {}).get("currency") or "EUR",
        "prices_are_incl_tax": (latest_invoice or {}).get("prices_are_incl_tax", False),
        "tax_rate_id": first_detail.get("tax_rate_id"),
        "ledger_account_id": first_detail.get("ledger_account_id"),
        "delivery_method": contact.get("delivery_method") or "Email",
        "send_invoices_to_email": contact.get("send_invoices_to_email") or contact.get("email") or "",
        "latest_invoice_id": (latest_invoice or {}).get("id"),
        "latest_invoice_number": (latest_invoice or {}).get("invoice_id"),
    }




def extract_invoice_discount(invoice: dict[str, Any]) -> Any:
    for key in ("discount", "discount_percentage", "discount_percent"):
        value = invoice.get(key)
        if value not in (None, "", []):
            return value
    return ""




def extract_invoice_extra_fields(invoice: dict[str, Any]) -> Any:
    for key in ("custom_fields", "extra_fields", "custom_field_values"):
        value = invoice.get(key)
        if value not in (None, "", []):
            return value
    return []




def merge_value(field: str, value: Any) -> Any:
    if field == "prices_are_incl_tax":
        return bool(value)
    if field == "extra_fields":
        return json.dumps(value or [], sort_keys=True, ensure_ascii=True)
    if value in (None, "", []):
        return ""
    return str(value)




def build_merge_snapshot_from_invoice(
    invoice: dict[str, Any],
    *,
    scheduled_send_on: str = "",
) -> dict[str, Any]:
    contact = invoice.get("contact") or {}
    return {
        "sales_invoice_id": str(invoice.get("id", "")),
        "invoice_id": invoice.get("invoice_id"),
        "contact_id": str(invoice.get("contact_id") or contact.get("id") or ""),
        "customer_id": contact.get("customer_id"),
        "scheduled_send_on": scheduled_send_on or str(invoice.get("invoice_date") or ""),
        "workflow_id": invoice.get("workflow_id"),
        "document_style_id": invoice.get("document_style_id"),
        "identity_id": invoice.get("identity_id"),
        "language": invoice.get("language"),
        "currency": invoice.get("currency"),
        "prices_are_incl_tax": invoice.get("prices_are_incl_tax", False),
        "discount": extract_invoice_discount(invoice),
        "extra_fields": extract_invoice_extra_fields(invoice),
        "state": invoice.get("state"),
    }




def build_merge_snapshot_from_payload(
    contact: dict[str, Any],
    sales_invoice: dict[str, Any],
    *,
    scheduled_send_on: str,
) -> dict[str, Any]:
    return {
        "sales_invoice_id": "",
        "invoice_id": None,
        "contact_id": str(sales_invoice.get("contact_id") or contact.get("id") or ""),
        "customer_id": contact.get("customer_id"),
        "scheduled_send_on": scheduled_send_on,
        "workflow_id": sales_invoice.get("workflow_id"),
        "document_style_id": sales_invoice.get("document_style_id"),
        "identity_id": sales_invoice.get("identity_id"),
        "language": sales_invoice.get("language"),
        "currency": sales_invoice.get("currency"),
        "prices_are_incl_tax": sales_invoice.get("prices_are_incl_tax", False),
        "discount": extract_invoice_discount(sales_invoice),
        "extra_fields": extract_invoice_extra_fields(sales_invoice),
        "state": sales_invoice.get("state"),
    }




def compare_merge_snapshots(
    reference: dict[str, Any],
    candidate: dict[str, Any],
) -> list[dict[str, Any]]:
    mismatches: list[dict[str, Any]] = []
    for field, label in MERGE_FIELD_LABELS.items():
        if merge_value(field, reference.get(field)) == merge_value(field, candidate.get(field)):
            continue
        mismatches.append(
            {
                "field": field,
                "label": label,
                "reference": reference.get(field),
                "candidate": candidate.get(field),
            }
        )
    return mismatches




def list_scheduled_merge_candidates(
    client: MoneybirdClient,
    *,
    contact_id: str,
    scheduled_send_on: str,
    exclude_sales_invoice_id: str = "",
) -> list[dict[str, Any]]:
    if not scheduled_send_on:
        return []

    invoices = list_contact_invoices_cached(
        client,
        contact_id=contact_id,
        period=year_period_for_date(scheduled_send_on),
    )
    candidates: list[dict[str, Any]] = []
    for invoice in invoices:
        invoice_id = str(invoice.get("id") or "")
        if not invoice_id or invoice_id == exclude_sales_invoice_id:
            continue
        if str(invoice.get("state") or "") != "scheduled":
            continue
        if str(invoice.get("invoice_date") or "") != scheduled_send_on:
            continue
        candidates.append(client.get_sales_invoice(invoice_id))
    return candidates




def align_defaults_with_scheduled_invoice(
    defaults: dict[str, Any],
    contact: dict[str, Any],
    scheduled_invoice: dict[str, Any],
) -> dict[str, Any]:
    details = scheduled_invoice.get("details") or [{}]
    first_detail = details[0]
    return {
        **defaults,
        "workflow_id": scheduled_invoice.get("workflow_id") or defaults.get("workflow_id"),
        "document_style_id": scheduled_invoice.get("document_style_id")
        or defaults.get("document_style_id"),
        "identity_id": scheduled_invoice.get("identity_id") or defaults.get("identity_id"),
        "language": scheduled_invoice.get("language") or defaults.get("language") or "nl",
        "currency": scheduled_invoice.get("currency") or defaults.get("currency"),
        "prices_are_incl_tax": scheduled_invoice.get(
            "prices_are_incl_tax",
            defaults.get("prices_are_incl_tax", False),
        ),
        "tax_rate_id": first_detail.get("tax_rate_id") or defaults.get("tax_rate_id"),
        "ledger_account_id": first_detail.get("ledger_account_id")
        or defaults.get("ledger_account_id"),
        "delivery_method": scheduled_invoice.get("delivery_method")
        or defaults.get("delivery_method")
        or contact.get("delivery_method")
        or "Email",
        "send_invoices_to_email": scheduled_invoice.get("email_address")
        or defaults.get("send_invoices_to_email")
        or contact.get("send_invoices_to_email")
        or contact.get("email")
        or "",
        "merge_reference_invoice_id": scheduled_invoice.get("id"),
        "merge_reference_invoice_number": scheduled_invoice.get("invoice_id"),
    }




def evaluate_merge_compatibility(
    proposed_snapshot: dict[str, Any],
    existing_invoices: list[dict[str, Any]],
) -> dict[str, Any]:
    scheduled_send_on = str(proposed_snapshot.get("scheduled_send_on") or "")
    result: dict[str, Any] = {
        "checked": bool(scheduled_send_on),
        "status": "not_scheduled" if not scheduled_send_on else "no_existing_candidates",
        "scheduled_send_on": scheduled_send_on,
        "matching_existing_invoices": [],
        "existing_candidates": [],
        "batch_group_mismatch_fields": [],
        "batch_group_compatible": None,
        "warnings": [],
    }
    if not scheduled_send_on:
        result["summary"] = "No automatic merge check because this invoice is not scheduled."
        return result

    for invoice in existing_invoices:
        reference = build_merge_snapshot_from_invoice(
            invoice,
            scheduled_send_on=scheduled_send_on,
        )
        mismatches = compare_merge_snapshots(reference, proposed_snapshot)
        comparison = {
            "sales_invoice_id": str(invoice.get("id") or ""),
            "invoice_id": invoice.get("invoice_id"),
            "mismatches": mismatches,
            "mismatch_fields": [item["label"] for item in mismatches],
        }
        result["existing_candidates"].append(comparison)
        if not mismatches:
            result["matching_existing_invoices"].append(
                {
                    "sales_invoice_id": comparison["sales_invoice_id"],
                    "invoice_id": comparison["invoice_id"],
                }
            )

    if result["matching_existing_invoices"]:
        result["status"] = "compatible"
        invoice_ids = ", ".join(
            item["invoice_id"] or item["sales_invoice_id"]
            for item in result["matching_existing_invoices"]
        )
        result["summary"] = (
            f"Merge-compatible with scheduled invoice(s) {invoice_ids} on {scheduled_send_on}."
        )
    elif existing_invoices:
        result["status"] = "warning"
        labels = sorted(
            {
                mismatch["label"]
                for candidate in result["existing_candidates"]
                for mismatch in candidate["mismatches"]
            }
        )
        result["warnings"].append(
            "Scheduled invoices already exist for this contact/date, but merge-sensitive "
            f"fields differ: {', '.join(labels)}."
        )
        result["summary"] = result["warnings"][0]
    else:
        result["summary"] = (
            f"No other scheduled invoice found for this contact on {scheduled_send_on}."
        )
    return result




def apply_batch_group_merge_checks(batch_items: list[dict[str, Any]]) -> None:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in batch_items:
        scheduled_send_on = str(item.get("schedule_send_on") or "")
        if not scheduled_send_on:
            continue
        key = (str(item["contact"]["id"]), scheduled_send_on)
        grouped.setdefault(key, []).append(item)

    for grouped_items in grouped.values():
        if len(grouped_items) <= 1:
            continue
        reference = grouped_items[0]["merge_snapshot"]
        mismatch_labels = sorted(
            {
                mismatch["label"]
                for item in grouped_items[1:]
                for mismatch in compare_merge_snapshots(reference, item["merge_snapshot"])
            }
        )
        for item in grouped_items:
            item["merge_check"]["batch_group_compatible"] = not mismatch_labels
            item["merge_check"]["batch_group_mismatch_fields"] = mismatch_labels
            if mismatch_labels:
                item["merge_check"]["warnings"].append(
                    "Prepared invoices for this contact/date differ on "
                    f"{', '.join(mismatch_labels)}, so Moneybird will not merge them."
                )
                item["merge_check"]["status"] = "warning"
                item["merge_check"]["summary"] = item["merge_check"]["warnings"][0]




def build_preview_row(
    *,
    customer_id: str,
    description: str,
    amount_excl_tax: Decimal,
    tax_percentage: Decimal,
    duplicate_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    tax_amount = (amount_excl_tax * tax_percentage / Decimal("100")).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    amount_incl_tax = (amount_excl_tax + tax_amount).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return {
        "customer_id": customer_id,
        "description": description,
        "amount_excl_tax": f"{amount_excl_tax:.2f}",
        "amount_tax": f"{tax_amount:.2f}",
        "amount_incl_tax": f"{amount_incl_tax:.2f}",
        "status": "duplicate-warning" if duplicate_hits else "ready",
    }


def build_invoice_line_preview(
    *,
    customer_id: str,
    description: str,
    entered_total: Decimal,
    tax_percentage: Decimal,
    prices_are_incl_tax: bool,
    duplicate_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build a preview that follows Moneybird's incl/excl price setting."""
    if prices_are_incl_tax:
        divisor = Decimal("1") + (tax_percentage / Decimal("100"))
        amount_incl_tax = entered_total
        amount_excl_tax = (
            entered_total / divisor if divisor else entered_total
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        tax_amount = amount_incl_tax - amount_excl_tax
        return {
            "customer_id": customer_id,
            "description": description,
            "amount_excl_tax": f"{amount_excl_tax:.2f}",
            "amount_tax": f"{tax_amount:.2f}",
            "amount_incl_tax": f"{amount_incl_tax:.2f}",
            "status": "duplicate-warning" if duplicate_hits else "ready",
        }
    return build_preview_row(
        customer_id=customer_id,
        description=description,
        amount_excl_tax=entered_total,
        tax_percentage=tax_percentage,
        duplicate_hits=duplicate_hits,
    )


def tax_rate_percentage(
    client: MoneybirdClient,
    tax_rate_id: str,
) -> Decimal:
    cache = getattr(client, "_moneybird_tax_rate_percentages", None)
    if cache is None:
        cache = {
            str(item.get("id")): Decimal(str(item.get("percentage") or "0"))
            for item in client.list_tax_rates()
            if item.get("id")
        }
        setattr(client, "_moneybird_tax_rate_percentages", cache)
    if tax_rate_id not in cache:
        raise MoneybirdError(f"Unknown tax_rate_id {tax_rate_id}.")
    return cache[tax_rate_id]




def find_potential_invoice_duplicates(
    client: MoneybirdClient,
    *,
    contact_id: str,
    invoice_date: str,
    reference: str,
    descriptions: list[str],
) -> list[dict[str, Any]]:
    invoices = list_contact_invoices_cached(
        client,
        contact_id=contact_id,
        period=year_period_for_date(invoice_date) if invoice_date else "this_year",
    )
    description_set = {normalized_text(item) for item in descriptions if item}
    duplicates: list[dict[str, Any]] = []
    for invoice in invoices:
        invoice_descriptions = {
            normalized_text(str(detail.get("description", "")))
            for detail in (invoice.get("details") or [])
            if detail.get("description")
        }
        same_reference = bool(reference) and (invoice.get("reference") or "") == reference
        same_date = bool(invoice_date) and invoice.get("invoice_date") == invoice_date
        same_description = bool(description_set & invoice_descriptions)
        if same_reference or (same_date and same_description):
            duplicates.append(
                {
                    "sales_invoice_id": str(invoice.get("id")),
                    "invoice_id": invoice.get("invoice_id"),
                    "state": invoice.get("state"),
                    "invoice_date": invoice.get("invoice_date"),
                    "reference": invoice.get("reference"),
                }
            )
    return duplicates




def build_batch_invoice_payload(
    client: MoneybirdClient,
    entry: dict[str, Any],
) -> dict[str, Any]:
    contact = resolve_contact_reference(
        client,
        contact_id=str(entry.get("contact_id", "")),
        customer_id=str(entry.get("customer_id", "")),
    )
    defaults = infer_contact_invoice_defaults(client, contact)
    prices_are_incl_tax = bool(
        entry.get("prices_are_incl_tax", defaults.get("prices_are_incl_tax", False))
    )
    schedule_send_on = str(entry.get("schedule_send_on", "")).strip()
    scheduled_merge_candidates = list_scheduled_merge_candidates(
        client,
        contact_id=str(contact["id"]),
        scheduled_send_on=schedule_send_on,
    )
    if len(scheduled_merge_candidates) == 1:
        defaults = align_defaults_with_scheduled_invoice(
            defaults,
            contact,
            scheduled_merge_candidates[0],
        )

    details_attributes = []
    descriptions: list[str] = []
    preview_rows: list[dict[str, Any]] = []
    for raw_detail in entry.get("details", []):
        description = str(raw_detail.get("description", "")).strip()
        if not description:
            raise MoneybirdError("Each invoice line needs a description.")

        amount = str(raw_detail.get("amount", "1")).strip()
        price = str(raw_detail.get("price", "")).strip()
        if not price:
            raise MoneybirdError("Each invoice line needs a price.")

        selected_tax_rate_id = str(
            raw_detail.get("tax_rate_id") or defaults.get("tax_rate_id") or ""
        )
        if not selected_tax_rate_id:
            raise MoneybirdError(
                f"No tax rate could be resolved for invoice line '{description}'."
            )
        tax_percentage = tax_rate_percentage(client, selected_tax_rate_id)
        supplied_percentage = raw_detail.get("tax_percentage")
        if supplied_percentage not in (None, ""):
            supplied = parse_decimal_number(supplied_percentage, label="tax_percentage")
            if supplied < 0 or supplied > 100:
                raise MoneybirdError("tax_percentage must be between 0 and 100.")
            if supplied != tax_percentage:
                raise MoneybirdError(
                    f"tax_percentage {supplied} does not match tax_rate_id "
                    f"{selected_tax_rate_id} ({tax_percentage}%)."
                )
        line_total = (
            parse_decimal_number(amount, label="amount")
            * parse_decimal_number(price, label="price")
        ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        preview_rows.append(
            build_invoice_line_preview(
                customer_id=str(contact.get("customer_id") or entry.get("customer_id") or contact.get("id")),
                description=description,
                entered_total=line_total,
                tax_percentage=tax_percentage,
                prices_are_incl_tax=prices_are_incl_tax,
                duplicate_hits=[],
            )
        )
        descriptions.append(description)
        details_attributes.append(
            clean_dict(
                {
                    "description": description,
                    "amount": amount,
                    "price": price,
                    "tax_rate_id": selected_tax_rate_id,
                    "ledger_account_id": raw_detail.get("ledger_account_id")
                    or defaults.get("ledger_account_id"),
                    "period": raw_detail.get("period", ""),
                }
            )
        )

    invoice_payload = clean_dict(
        {
            "contact_id": str(contact["id"]),
            "workflow_id": entry.get("workflow_id") or defaults.get("workflow_id"),
            "document_style_id": entry.get("document_style_id") or defaults.get("document_style_id"),
            "identity_id": entry.get("identity_id") or defaults.get("identity_id"),
            "language": entry.get("language") or defaults.get("language"),
            "reference": entry.get("reference", ""),
            "invoice_date": entry.get("invoice_date", ""),
            "due_date": entry.get("due_date", ""),
            "currency": entry.get("currency") or defaults.get("currency"),
            "prices_are_incl_tax": prices_are_incl_tax,
            "details_attributes": details_attributes,
        }
    )
    duplicates = find_potential_invoice_duplicates(
        client,
        contact_id=str(contact["id"]),
        invoice_date=str(invoice_payload.get("invoice_date", "")),
        reference=str(invoice_payload.get("reference", "")),
        descriptions=descriptions,
    )
    for row in preview_rows:
        row["status"] = "duplicate-warning" if duplicates else "ready"

    send_payload = clean_dict(
        {
            "sending_scheduled": bool(schedule_send_on),
            "invoice_date": schedule_send_on,
            "delivery_method": entry.get("delivery_method") or defaults.get("delivery_method"),
            "email_address": entry.get("email_address") or defaults.get("send_invoices_to_email"),
            "email_message": entry.get("email_message", ""),
        }
    )
    merge_snapshot = build_merge_snapshot_from_payload(
        contact,
        invoice_payload,
        scheduled_send_on=schedule_send_on,
    )
    merge_check = evaluate_merge_compatibility(
        merge_snapshot,
        scheduled_merge_candidates,
    )
    if defaults.get("merge_reference_invoice_id"):
        merge_check["defaults_aligned_from_invoice"] = {
            "sales_invoice_id": str(defaults.get("merge_reference_invoice_id")),
            "invoice_id": defaults.get("merge_reference_invoice_number"),
        }

    return {
        "contact": {
            "id": str(contact["id"]),
            "customer_id": contact.get("customer_id"),
            "title": contact_title(contact),
        },
        "defaults": defaults,
        "sales_invoice": invoice_payload,
        "send_payload": send_payload,
        "schedule_send_on": schedule_send_on,
        "duplicates": duplicates,
        "preview_rows": preview_rows,
        "expected_total_incl_tax": f"{sum(Decimal(row['amount_incl_tax']) for row in preview_rows):.2f}",
        "merge_snapshot": merge_snapshot,
        "merge_check": merge_check,
    }




def summarize_batch_preview(batch_items: list[dict[str, Any]]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    merge_checks: list[dict[str, Any]] = []
    for item in batch_items:
        item_status = "ready"
        if item["duplicates"]:
            item_status = "duplicate-warning"
        if item["merge_check"]["status"] == "warning":
            item_status = (
                "duplicate+merge-warning"
                if item_status == "duplicate-warning"
                else "merge-warning"
            )
        for row in item["preview_rows"]:
            row["status"] = item_status
        rows.extend(item["preview_rows"])
        duplicates.extend(
            [
                {"customer_id": item["contact"].get("customer_id"), **duplicate}
                for duplicate in item["duplicates"]
            ]
        )
        merge_checks.append(
            {
                "customer_id": item["contact"].get("customer_id"),
                "scheduled_send_on": item["merge_check"].get("scheduled_send_on"),
                "status": item["merge_check"].get("status"),
                "summary": item["merge_check"].get("summary"),
                "warnings": item["merge_check"].get("warnings"),
                "matching_existing_invoices": item["merge_check"].get(
                    "matching_existing_invoices",
                    [],
                ),
                "defaults_aligned_from_invoice": item["merge_check"].get(
                    "defaults_aligned_from_invoice"
                ),
            }
        )
    return {
        "preview_table": render_preview_table(rows),
        "rows": rows,
        "duplicate_count": len(duplicates),
        "duplicates": duplicates,
        "merge_warning_count": sum(
            1 for item in batch_items if item["merge_check"].get("status") == "warning"
        ),
        "merge_checks": merge_checks,
    }




def find_contact_matches(
    client: MoneybirdClient,
    *,
    query: str,
    limit: int = 10,
) -> list[dict[str, Any]]:
    contacts = client.list_contacts(limit=100, page=1)
    matches: list[dict[str, Any]] = []
    for contact in contacts:
        text = normalize_text(
            contact.get("company_name"),
            contact.get("firstname"),
            contact.get("lastname"),
            contact.get("email"),
            contact.get("customer_id"),
            contact.get("phone"),
            contact.get("city"),
        )
        if matches_query(text, query):
            matches.append(
                {
                    "id": str(contact.get("id")),
                    "title": contact_title(contact),
                    "customer_id": contact.get("customer_id"),
                    "email": contact.get("email"),
                    "phone": contact.get("phone"),
                    "city": contact.get("city"),
                    "url": api_url("contacts", str(contact.get("id")), client.administration_id),
                }
            )
    return matches[: max(1, min(limit, 25))]




def prepare_general_journal_entries(
    client: MoneybirdClient,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    resolved_entries: list[dict[str, Any]] = []
    preview_entries: list[dict[str, Any]] = []
    for entry in entries:
        ledger_account = resolve_ledger_account_reference(
            client,
            ledger_account_id=str(entry.get("ledger_account_id", "")),
            ledger_account_name=str(entry.get("ledger_account_name", "")),
        )
        resolved_entry = clean_dict(
            {
                "ledger_account_id": str(ledger_account.get("id")),
                "tax_rate_id": entry.get("tax_rate_id"),
                "description": entry.get("description", ""),
                "contact_id": entry.get("contact_id"),
                "project_id": entry.get("project_id"),
                "debit": entry.get("debit", 0),
                "credit": entry.get("credit", 0),
            }
        )
        resolved_entries.append(resolved_entry)
        preview_entries.append(
            {
                "ledger_account_id": str(ledger_account.get("id")),
                "ledger_account_name": ledger_account.get("name"),
                "debit": str(entry.get("debit", 0)),
                "credit": str(entry.get("credit", 0)),
                "description": entry.get("description", ""),
            }
        )
    validated = validate_general_journal_entries(resolved_entries)
    return {
        **validated,
        "preview_entries": preview_entries,
    }




def prepare_reclassification_batch(
    client: MoneybirdClient,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    if not entries:
        raise MoneybirdError("Provide at least one document line update.")

    document_cache: dict[tuple[str, str], dict[str, Any]] = {}
    prepared_updates: dict[tuple[str, str], dict[str, Any]] = {}
    preview_rows: list[dict[str, Any]] = []
    journal_previews: list[dict[str, Any]] = []
    journal_payloads: list[dict[str, Any]] = []
    seen_detail_keys: set[tuple[str, str, str]] = set()
    seen_journal_references: set[str] = set()

    for entry in entries:
        kind = normalize_document_kind(
            str(entry.get("document_kind") or entry.get("document_type") or "")
        )
        if kind not in {"purchase_invoice", "receipt"}:
            raise MoneybirdError(
                "prepare_reclassify_document_lines currently supports purchase_invoice and receipt documents."
            )
        document_id = str(entry.get("document_id", "")).strip()
        if not document_id:
            raise MoneybirdError("Each line update needs document_id.")

        cache_key = (kind, document_id)
        document = document_cache.get(cache_key)
        if document is None:
            document = client.get_document(kind, document_id)
            document_cache[cache_key] = document

        target_account = resolve_ledger_account_reference(
            client,
            ledger_account_id=str(entry.get("ledger_account_id", "")),
            ledger_account_name=str(entry.get("ledger_account_name", "")),
        )
        validate_document_ledger_target(kind, target_account)
        row_order_raw = entry.get("row_order")
        row_order = int(row_order_raw) if row_order_raw not in (None, "") else None
        detail = resolve_document_detail(
            document,
            detail_id=str(entry.get("detail_id", "")),
            row_order=row_order,
        )
        detail_key = (kind, document_id, str(detail.get("id")))
        if detail_key in seen_detail_keys:
            raise MoneybirdError(
                f"Document detail {detail.get('id')} on {kind} {document_id} is listed more than once."
            )
        seen_detail_keys.add(detail_key)

        prepared_update = prepared_updates.setdefault(
            cache_key,
            {
                "document_kind": kind,
                "document_id": document_id,
                "document_title": purchase_document_title(kind, document),
                "details_attributes": [],
            },
        )
        prepared_update["details_attributes"].append(
            {
                "id": str(detail.get("id")),
                "ledger_account_id": str(target_account.get("id")),
            }
        )

        amount_excl_tax = document_detail_amount_excl_tax(detail)
        preview_rows.append(
            {
                "customer_id": str(document.get("reference") or document.get("entry_number") or document_id),
                "description": (
                    f"{detail.get('description') or 'document line'} -> {target_account.get('name')}"
                ),
                "amount_excl_tax": f"{amount_excl_tax:.2f}",
                "amount_tax": "",
                "amount_incl_tax": "",
                "status": "ready",
            }
        )

        balancing_requested = any(
            entry.get(field) not in (None, "")
            for field in ("balancing_ledger_account_id", "balancing_ledger_account_name")
        )
        if not balancing_requested:
            continue

        balancing_account = resolve_ledger_account_reference(
            client,
            ledger_account_id=str(entry.get("balancing_ledger_account_id", "")),
            ledger_account_name=str(entry.get("balancing_ledger_account_name", "")),
        )
        if amount_excl_tax == Decimal("0.00"):
            raise MoneybirdError(
                f"Cannot create a balancing journal for document detail {detail.get('id')} with zero amount."
            )
        journal_reference = str(entry.get("journal_reference", "")).strip()
        if not journal_reference:
            raise MoneybirdError(
                "journal_reference is required when balancing_ledger_account_id or balancing_ledger_account_name is provided."
            )
        if journal_reference in seen_journal_references:
            raise MoneybirdError(f"journal_reference '{journal_reference}' is duplicated.")
        seen_journal_references.add(journal_reference)

        journal_date = str(entry.get("journal_date") or document.get("date") or "").strip()
        if not journal_date:
            raise MoneybirdError(
                f"journal_date is required for balancing journal {journal_reference}."
            )
        journal_description = str(
            entry.get("journal_description")
            or detail.get("description")
            or purchase_document_title(kind, document)
        ).strip()
        amount_abs = abs(amount_excl_tax)
        if amount_excl_tax >= 0:
            journal_entries = [
                {
                    "ledger_account_id": str(balancing_account.get("id")),
                    "debit": str(amount_abs),
                    "credit": "0.00",
                    "description": journal_description,
                },
                {
                    "ledger_account_id": str(target_account.get("id")),
                    "debit": "0.00",
                    "credit": str(amount_abs),
                    "description": journal_description,
                },
            ]
        else:
            journal_entries = [
                {
                    "ledger_account_id": str(target_account.get("id")),
                    "debit": str(amount_abs),
                    "credit": "0.00",
                    "description": journal_description,
                },
                {
                    "ledger_account_id": str(balancing_account.get("id")),
                    "debit": "0.00",
                    "credit": str(amount_abs),
                    "description": journal_description,
                },
            ]
        validated_journal_entries = validate_general_journal_entries(journal_entries)
        general_journal_document = clean_dict(
            {
                "reference": journal_reference,
                "date": journal_date,
                "description": journal_description,
                "general_journal_document_entries_attributes": details_attributes_payload(
                    validated_journal_entries["entries"]
                ),
            }
        )
        journal_payloads.append(
            {
                "reference": journal_reference,
                "general_journal_document": general_journal_document,
            }
        )
        journal_previews.append(
            {
                "reference": journal_reference,
                "date": journal_date,
                "description": journal_description,
                "target_ledger_account": target_account.get("name"),
                "balancing_ledger_account": balancing_account.get("name"),
                "amount_excl_tax": f"{amount_excl_tax:.2f}",
            }
        )

    document_updates = list(prepared_updates.values())
    for update in document_updates:
        document = document_cache[
            (update["document_kind"], update["document_id"])
        ]
        update["precondition"] = build_patch_precondition(
            document,
            {"details_attributes": update["details_attributes"]},
        )
    payload = {
        "document_updates": document_updates,
        "general_journal_documents": journal_payloads,
    }
    fingerprint = duplicate_fingerprint("reclassify_document_lines", payload)
    return {
        "payload": {**payload, "fingerprint": fingerprint},
        "preview": {
            "preview_table": render_preview_table(preview_rows),
            "rows": preview_rows,
            "document_count": len(document_updates),
            "line_count": len(entries),
            "general_journal_count": len(journal_payloads),
            "general_journal_documents": journal_previews,
        },
        "fingerprint": fingerprint,
    }
