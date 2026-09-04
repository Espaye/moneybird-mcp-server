"""Pure formatting, normalization, and record-shaping helpers (no network)."""
from __future__ import annotations

import hashlib
import json
import re
from calendar import monthrange
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .config import (
    BASE_URL,
    DOCUMENT_KIND_ALIASES,
    DOCUMENT_KIND_CONFIG,
    MAX_ERROR_DETAIL_CHARS,
    MoneybirdError,
)


def parse_reported_error(body: str | None) -> Any:
    """Return the useful part of a Moneybird error body, or None.

    A rejected write answers with the field-level reason, e.g.::

        {"error": {"send_invoices_to_email": ["includes a domain which cannot
         receive emails"]}, "details": {...}}

    Without it, "HTTP 422" tells nobody which field to correct. Anything that is
    not JSON, or is JSON of an unexpected shape, is returned as-is so the caller
    can still quote it; an empty body returns None.
    """
    text = (body or "").strip()
    if not text:
        return None
    try:
        decoded = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(decoded, dict):
        # Prefer the named error over the whole envelope; "details" repeats it
        # as machine codes, which add length without adding meaning for a reader.
        for key in ("error", "errors", "message"):
            if key in decoded and decoded[key]:
                return decoded[key]
    return decoded


def format_reported_error(reported: Any) -> str:
    """Render a parsed Moneybird error as a bounded sentence to append.

    Returns "" when there is nothing to say, so callers can concatenate it
    unconditionally.
    """
    if reported is None or reported == "" or reported == {} or reported == []:
        return ""
    if isinstance(reported, dict):
        parts = []
        for field, problem in reported.items():
            if isinstance(problem, (list, tuple)):
                problem = "; ".join(str(item) for item in problem)
            parts.append(f"{field}: {problem}")
        rendered = " | ".join(parts)
    elif isinstance(reported, (list, tuple)):
        rendered = "; ".join(str(item) for item in reported)
    else:
        rendered = str(reported)
    rendered = " ".join(rendered.split())
    if not rendered:
        return ""
    if len(rendered) > MAX_ERROR_DETAIL_CHARS:
        rendered = rendered[:MAX_ERROR_DETAIL_CHARS] + " [truncated]"
    return f" Moneybird reported: {rendered}"


def api_url(resource: str, item_id: str, administration_id: str | None) -> str | None:
    if not administration_id:
        return None
    return f"{BASE_URL}/{administration_id}/{resource}/{item_id}.json"




def contact_title(contact: dict[str, Any]) -> str:
    company = (contact.get("company_name") or "").strip()
    person = " ".join(
        part.strip()
        for part in [contact.get("firstname") or "", contact.get("lastname") or ""]
        if part
    ).strip()

    if company and person:
        return f"Contact: {company} ({person})"
    if company:
        return f"Contact: {company}"
    if person:
        return f"Contact: {person}"
    return f'Contact: {contact.get("id", "unknown")}'




def invoice_title(invoice: dict[str, Any]) -> str:
    invoice_number = invoice.get("invoice_id") or invoice.get("id")
    state = invoice.get("state") or "unknown"
    total = invoice.get("total_price_incl_tax") or invoice.get("price") or "unknown"
    return f"Sales invoice {invoice_number} ({state}, {total})"




def stringify_record(record: dict[str, Any]) -> str:
    return json.dumps(record, indent=2, sort_keys=True, ensure_ascii=True)




def normalize_text(*values: Any) -> str:
    return " ".join(str(value) for value in values if value).casefold()




def matches_query(record_text: str, query: str) -> bool:
    normalized_query = query.casefold().strip()
    return normalized_query in record_text if normalized_query else False




def chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[index : index + size] for index in range(0, len(items), size)]




def iso_now() -> str:
    return datetime.now(UTC).isoformat()




def clean_dict(data: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in data.items()
        if value is not None and value != "" and value != []
    }




def normalize_document_kind(kind: str) -> str:
    normalized = DOCUMENT_KIND_ALIASES.get(str(kind).strip())
    if not normalized:
        supported = ", ".join(sorted(DOCUMENT_KIND_ALIASES))
        raise MoneybirdError(
            f"Unsupported document kind '{kind}'. Use one of: {supported}."
        )
    return normalized




def document_kind_config(kind: str) -> dict[str, str]:
    return DOCUMENT_KIND_CONFIG[normalize_document_kind(kind)]




def normalize_list_period(period: str) -> str:
    """Expand a bare ``YYYYMM`` period into the day range list endpoints accept.

    Moneybird's *report* endpoints take a bare month ('202601'), but the
    collection endpoints do not: they answer HTTP 400 ('Period is invalid', or
    'Period must have both a start and end date' on sales invoices). Callers
    reasonably assume one period syntax spans the whole API, so a bare month is
    widened here to the equivalent explicit range rather than being sent on to
    be rejected. Symbolic periods ('this_month'), day ranges, and anything
    unparseable are returned untouched for the server to judge.
    """
    text = str(period or "").strip()
    if not text:
        return text
    if ".." in text:
        start_text, end_text = (side.strip() for side in text.split("..", 1))
        start = _widen_month_endpoint(start_text, to_end=False)
        end = _widen_month_endpoint(end_text, to_end=True)
        if start is None or end is None:
            return text
        return f"{start}..{end}"
    widened = _widen_month_endpoint(text, to_end=False)
    if widened is None or not _YEAR_MONTH.fullmatch(text):
        return text
    return f"{widened}..{_widen_month_endpoint(text, to_end=True)}"


def _widen_month_endpoint(text: str, *, to_end: bool) -> str | None:
    """Return one period endpoint as ``YYYYMMDD``, or None when unparseable.

    A day endpoint is already precise and passes through; a bare month opens on
    the 1st and closes on its own last day, so a month range covers exactly the
    months asked for.
    """
    if _YEAR_MONTH_DAY.fullmatch(text):
        return text
    endpoint = _period_endpoint_month(text)
    if not endpoint or not _YEAR_MONTH.fullmatch(text):
        return None
    year, month = endpoint
    day = monthrange(year, month)[1] if to_end else 1
    return f"{year}{month:02d}{day:02d}"


def build_filter_string(*, filter: str = "", period: str = "") -> str:
    parts = [part.strip() for part in str(filter or "").split(",") if part.strip()]
    # A period passed inside the raw filter string reaches Moneybird just as a
    # bare month does, so normalize both spellings, not only the keyword.
    parts = [
        f"period:{normalize_list_period(part[len('period:'):])}"
        if part.startswith("period:")
        else part
        for part in parts
    ]
    if period and not any(part.startswith("period:") for part in parts):
        parts.append(f"period:{normalize_list_period(period)}")
    return ",".join(parts)




def document_url(kind: str, item_id: str, administration_id: str | None) -> str | None:
    if not administration_id:
        return None
    config = document_kind_config(kind)
    return f"{BASE_URL}/{administration_id}/{config['collection_path']}/{item_id}.json"




def document_contact_title(document: dict[str, Any]) -> str:
    contact = document.get("contact") or {}
    company = (contact.get("company_name") or "").strip()
    person = " ".join(
        part.strip()
        for part in [contact.get("firstname") or "", contact.get("lastname") or ""]
        if part
    ).strip()
    return company or person




def purchase_document_title(kind: str, document: dict[str, Any]) -> str:
    config = document_kind_config(kind)
    reference = document.get("reference") or document.get("entry_number") or document.get("id")
    contact = document_contact_title(document) or "unknown contact"
    total = document.get("total_price_incl_tax") or document.get("total_price_excl_tax") or "unknown"
    return f"{config['label'].title()} {reference} ({contact}, {total})"




def general_journal_title(document: dict[str, Any]) -> str:
    reference = document.get("reference") or document.get("id")
    date = document.get("date") or "unknown date"
    return f"General journal {reference} ({date})"




def financial_mutation_title(mutation: dict[str, Any]) -> str:
    counterparty = mutation.get("contra_account_name") or mutation.get("message") or "unknown"
    amount = mutation.get("amount") or "unknown"
    date = mutation.get("date") or "unknown date"
    return f"Financial mutation {counterparty} ({date}, {amount})"




def monetary_text(value: Any) -> str:
    if value in (None, ""):
        return ""
    return str(value)




def record_contact_id(record: dict[str, Any]) -> str:
    """The contact a document or invoice belongs to, however it is nested."""
    return str(
        (record.get("contact") or {}).get("id")
        or record.get("contact_id")
        or ""
    )


def record_facets(record: dict[str, Any]) -> dict[str, str]:
    """The few fields a caller almost always needs next after a search hit.

    A bare id/title/url hit forces a follow-up ``fetch`` per result just to learn
    the amount or the state, and ``fetch`` returns the whole raw record (~9.7 KB
    for one sales invoice). Carrying these four cheap fields on the hit itself
    removes that round trip for the common "which of these is the one?" question.
    They are also what makes the index answerable by contact without a scan.
    """
    return {
        "contact_id": record_contact_id(record),
        "date": str(record.get("invoice_date") or record.get("date") or ""),
        "amount": str(record.get("total_price_incl_tax") or ""),
        "state": str(record.get("state") or ""),
    }


def search_hit(record: dict[str, Any]) -> dict[str, Any]:
    """Project a stored search record onto the result shape returned to callers.

    Carries the facets (see :func:`record_facets`) but never ``search_text``,
    which is a normalised match blob and not useful to a reader.
    """
    hit = {
        "id": record.get("id"),
        "title": record.get("title"),
        "url": record.get("url"),
    }
    for key in ("contact_id", "date", "amount", "state", "description"):
        value = record.get(key)
        if value:
            hit[key] = value
    return hit


def document_search_record(kind: str, document: dict[str, Any], administration_id: str | None) -> dict[str, Any]:
    config = document_kind_config(kind)
    document_id = str(document.get("id"))
    return {
        "id": f"{config['id_prefix']}:{document_id}",
        "title": purchase_document_title(kind, document),
        "url": document_url(kind, document_id, administration_id),
        **record_facets(document),
        "search_text": normalize_text(
            document.get("reference"),
            document.get("entry_number"),
            document.get("date"),
            document_contact_title(document),
            document.get("total_price_incl_tax"),
            document.get("total_price_excl_tax"),
            *(detail.get("description") for detail in document.get("details") or []),
        ),
    }




def general_journal_search_record(document: dict[str, Any], administration_id: str | None) -> dict[str, Any]:
    document_id = str(document.get("id"))
    return {
        "id": f"general_journal_document:{document_id}",
        "title": general_journal_title(document),
        "url": document_url("general_journal_document", document_id, administration_id),
        **record_facets(document),
        "search_text": normalize_text(
            document.get("reference"),
            document.get("date"),
            *(entry.get("description") for entry in document.get("general_journal_document_entries") or []),
        ),
    }




# A bank narrative is one line on a statement; the cap keeps a pathological one
# from dominating a search result while leaving room for a policy number plus the
# period it covers.
MAX_BANK_DESCRIPTION_CHARS = 200


def truncate_text(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def bank_description(mutation: dict[str, Any]) -> str:
    """The bank's own narrative for a mutation, untruncated.

    Moneybird keeps the SEPA remittance information in ``sepa_fields.remi``: the
    text the statement shows, e.g. ``"POL polis 000000000111 Periode 01.02.2026 -
    01.05.2026"``. For a recurring direct debit that is routinely the *only*
    field naming the contract and the period it covers — ``contra_account_name``
    is just the insurer, and two unrelated policies from one insurer are
    otherwise indistinguishable. ``message`` is the fallback for feeds that carry
    no SEPA fields.
    """
    sepa = mutation.get("sepa_fields")
    remi = str(sepa.get("remi") or "").strip() if isinstance(sepa, dict) else ""
    return remi or str(mutation.get("message") or "").strip()


def financial_mutation_search_record(mutation: dict[str, Any], administration_id: str | None) -> dict[str, Any]:
    mutation_id = str(mutation.get("id"))
    sepa = mutation.get("sepa_fields")
    sepa_reference = str(sepa.get("eref") or "").strip() if isinstance(sepa, dict) else ""
    description = bank_description(mutation)
    return {
        "id": f"financial_mutation:{mutation_id}",
        "title": financial_mutation_title(mutation),
        "url": api_url("financial_mutations", mutation_id, administration_id),
        # A mutation has no contact; amount is its own field rather than a total.
        "contact_id": "",
        "date": str(mutation.get("date") or ""),
        "amount": str(mutation.get("amount") or ""),
        "state": str(mutation.get("state") or ""),
        "description": truncate_text(description, MAX_BANK_DESCRIPTION_CHARS),
        "search_text": normalize_text(
            mutation.get("date"),
            mutation.get("amount"),
            mutation.get("message"),
            # Match on the full narrative, not the display-truncated copy.
            description,
            # eref is the payer's end-to-end reference and is worth matching;
            # sref is an opaque scheme UUID that would only add noise.
            sepa_reference,
            mutation.get("contra_account_name"),
            mutation.get("contra_account_number"),
            *(booking.get("description") for booking in mutation.get("ledger_account_bookings") or []),
        ),
    }




def compact_document_summary(
    kind: str,
    document: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any]:
    config = document_kind_config(kind)
    document_id = str(document.get("id"))
    details = document.get("details") or []
    return {
        "id": document_id,
        "kind": config["id_prefix"],
        "title": purchase_document_title(kind, document),
        "reference": document.get("reference"),
        "date": document.get("date"),
        "state": document.get("state"),
        "contact_name": document_contact_title(document),
        "contact_id": str(document.get("contact_id") or ""),
        "total_price_excl_tax": document.get("total_price_excl_tax"),
        "total_price_incl_tax": document.get("total_price_incl_tax"),
        "payments_count": len(document.get("payments") or []),
        "details_count": len(details),
        "url": document_url(kind, document_id, administration_id),
    }




def compact_general_journal_summary(
    document: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any]:
    document_id = str(document.get("id"))
    entries = document.get("general_journal_document_entries") or []
    return {
        "id": document_id,
        "kind": "general_journal_document",
        "title": general_journal_title(document),
        "reference": document.get("reference"),
        "date": document.get("date"),
        "state": document.get("state"),
        "entries_count": len(entries),
        "url": document_url("general_journal_document", document_id, administration_id),
    }




def compact_financial_mutation_summary(
    mutation: dict[str, Any],
    administration_id: str | None,
    financial_account: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mutation_id = str(mutation.get("id"))
    financial_account = financial_account or mutation.get("financial_account") or {}
    payments = mutation.get("payments") or []
    ledger_bookings = mutation.get("ledger_account_bookings") or []
    return {
        "id": mutation_id,
        "kind": "financial_mutation",
        "title": financial_mutation_title(mutation),
        "date": mutation.get("date"),
        "state": mutation.get("state"),
        "settlement_state": mutation.get("settlement_state"),
        "amount": mutation.get("amount"),
        "amount_open": mutation.get("amount_open"),
        "message": mutation.get("message"),
        "contra_account_name": mutation.get("contra_account_name"),
        "contra_account_number": mutation.get("contra_account_number"),
        "financial_account_id": str(mutation.get("financial_account_id") or ""),
        "financial_account_name": (
            financial_account.get("name")
            or financial_account.get("identifier")
            or financial_account.get("iban")
            or mutation.get("financial_account_name")
            or mutation.get("financial_account_identifier")
        ),
        "bookings_count": len(payments) + len(ledger_bookings),
        "url": api_url("financial_mutations", mutation_id, administration_id),
    }




def compact_ledger_account_summary(account: dict[str, Any]) -> dict[str, Any]:
    taxonomy_item = account.get("taxonomy_item") or {}
    return {
        "id": str(account.get("id")),
        "name": account.get("name"),
        "account_type": account.get("account_type"),
        "account_id": account.get("account_id"),
        "active": account.get("active"),
        "rgs_code": taxonomy_item.get("code"),
        "rgs_name": taxonomy_item.get("name"),
        "rgs_taxonomy_version": taxonomy_item.get("taxonomy_version"),
    }




def compact_financial_account_summary(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(account.get("id")),
        "name": account.get("name"),
        "account_type": account.get("type"),
        "identifier": account.get("identifier"),
        "currency": account.get("currency"),
        "active": account.get("active"),
    }




def report_title(report_name: str, period: str) -> str:
    return f"{str(report_name).replace('_', ' ').title()} ({period})"




def contact_invoice_email(contact: dict[str, Any]) -> str:
    return str(contact.get("send_invoices_to_email") or contact.get("email") or "").strip()




def contact_delivery_record(
    contact: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any]:
    contact_id = str(contact.get("id") or "")
    return {
        "contact_id": contact_id,
        "customer_id": contact.get("customer_id"),
        "title": contact_title(contact),
        "delivery_method": contact.get("delivery_method") or "",
        "email": contact.get("email") or "",
        "send_invoices_to_email": contact.get("send_invoices_to_email") or "",
        "invoice_email": contact_invoice_email(contact),
        "archived": bool(contact.get("archived")),
        "url": api_url("contacts", contact_id, administration_id) if contact_id else None,
    }




def contact_search_record(
    contact: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any]:
    record_id = str(contact.get("id"))
    archived = bool(contact.get("archived"))
    return {
        "id": f"contact:{record_id}",
        "kind": "contact",
        # Archived contacts are indexed (see MoneybirdClient.list_contact_versions),
        # so the hit itself must say so: a supplier you stopped using still owns
        # its historical invoices, but cannot be invoiced again without being
        # unarchived first. Marking it in the title costs no FTS column and keeps
        # the distinction visible wherever the hit is rendered.
        "title": f"{contact_title(contact)} [gearchiveerd]" if archived else contact_title(contact),
        "url": api_url("contacts", record_id, administration_id),
        # A contact is its own counterparty; the remaining facets do not apply.
        "contact_id": record_id,
        "date": "",
        "amount": "",
        "state": "archived" if archived else "",
        "search_text": normalize_text(
            contact.get("company_name"),
            contact.get("firstname"),
            contact.get("lastname"),
            contact.get("email"),
            contact.get("customer_id"),
            contact.get("phone"),
            contact.get("city"),
            # Both spellings, so "gearchiveerde leveranciers" and "archived"
            # each reach these records.
            "gearchiveerd archived" if archived else "",
        ),
    }




def sales_invoice_search_record(
    invoice: dict[str, Any],
    administration_id: str | None,
) -> dict[str, Any]:
    record_id = str(invoice.get("id"))
    details = invoice.get("details") or []
    return {
        "id": f"sales_invoice:{record_id}",
        "kind": "sales_invoice",
        "title": invoice_title(invoice),
        "url": api_url("sales_invoices", record_id, administration_id),
        **record_facets(invoice),
        "search_text": normalize_text(
            invoice.get("invoice_id"),
            invoice.get("reference"),
            invoice.get("state"),
            invoice.get("invoice_date"),
            invoice.get("contact", {}).get("company_name"),
            invoice.get("contact", {}).get("firstname"),
            invoice.get("contact", {}).get("lastname"),
            invoice.get("contact", {}).get("customer_id"),
            " ".join(str(detail.get("description", "")) for detail in details),
        ),
    }




def money_decimal(value: Any) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)




# A line quantity written as a number followed by unit noise ("1 x", "3 stuks").
# The trailing part must not contain digits, so an ambiguous decimal separator
# ("1,5") falls through to an explicit refusal instead of being truncated.
_LINE_QUANTITY = re.compile(r"^(-?[0-9]+(?:\.[0-9]+)?)\s*([^0-9]*)$")


def document_line_quantity(value: Any) -> Decimal:
    """Return the effective quantity for a Moneybird document line ``amount``.

    Older lines carry values like ``"1 x"`` or ``""`` where a bare number is
    expected, and Moneybird itself treats both as a quantity of one. Values that
    are neither blank nor an unambiguous number are refused rather than guessed:
    this quantity scales a line total, so a silent wrong reading would turn into
    a wrong amount further down.
    """
    if value is None:
        return Decimal("1")
    if isinstance(value, bool):
        raise MoneybirdError(f"Document line amount {value!r} is not a quantity.")
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, Decimal):
        return value
    if isinstance(value, float):
        return Decimal(str(value))

    text = str(value).strip()
    if not text:
        return Decimal("1")
    match = _LINE_QUANTITY.match(text)
    if not match:
        raise MoneybirdError(
            f"Document line amount {value!r} is not an unambiguous quantity. "
            "Moneybird reads a blank amount or a value like '1 x' as 1, but this "
            "one cannot be read that way -- correct the line in Moneybird (a "
            "decimal comma has to be written as '1.5') and try again."
        )
    return Decimal(match.group(1))




_YEAR_MONTH = re.compile(r"^([0-9]{4})(0[1-9]|1[0-2])$")
_YEAR_MONTH_DAY = re.compile(r"^([0-9]{4})(0[1-9]|1[0-2])(0[1-9]|[12][0-9]|3[01])$")


def _period_endpoint_month(text: str) -> tuple[int, int] | None:
    """Return the (year, month) an explicit period endpoint falls in."""
    match = _YEAR_MONTH_DAY.match(text) or _YEAR_MONTH.match(text)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def report_period_months(period: str) -> list[str] | None:
    """Return the ``YYYYMM`` months an explicit report period covers.

    Returns ``None`` when the period is blank or symbolic ('this_month'), since
    those resolve server-side. This is deliberately separate from
    ``vat_settlement.month_periods``, which enforces the much stricter
    whole-month range a settlement needs; here the job is only to count how many
    months a caller asked for.
    """
    text = str(period or "").strip()
    if not text:
        return None

    if ".." not in text:
        start = _period_endpoint_month(text)
        return [f"{start[0]}{start[1]:02d}"] if start else None

    start_text, end_text = text.split("..", 1)
    start = _period_endpoint_month(start_text.strip())
    end = _period_endpoint_month(end_text.strip())
    if not start or not end or start > end:
        return None

    months: list[str] = []
    year, month = start
    while (year, month) <= end:
        months.append(f"{year}{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def symbolic_period_months(period: str) -> list[str] | None:
    """Return the ``YYYYMM`` months a year-wide symbolic period covers.

    ``report_period_months`` deliberately returns None for symbolic periods
    because the server resolves them. Splitting a rejected request needs the
    concrete months anyway, so the two year symbols are resolved here. Months
    after the current one are dropped for the running year: they cannot hold
    records yet, and each would still cost a request.
    """
    text = str(period or "").strip().casefold()
    now = datetime.now()
    if text == "this_year":
        return [f"{now.year}{month:02d}" for month in range(1, now.month + 1)]
    if text == "prev_year":
        return [f"{now.year - 1}{month:02d}" for month in range(1, 13)]
    return None


def normalized_text(value: str) -> str:
    return " ".join(value.casefold().split())




def year_period_for_date(invoice_date: str) -> str:
    if len(invoice_date) >= 4 and invoice_date[:4].isdigit():
        year = invoice_date[:4]
        return f"{year}0101..{year}1231"
    return "this_year"




def render_preview_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no rows)"

    headers = ["customer", "description", "excl", "btw", "incl", "status"]
    table_rows = [
        [
            str(row.get("customer_id", "")),
            str(row.get("description", "")),
            str(row.get("amount_excl_tax", "")),
            str(row.get("amount_tax", "")),
            str(row.get("amount_incl_tax", "")),
            str(row.get("status", "")),
        ]
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(line[index]) for line in table_rows))
        for index in range(len(headers))
    ]
    header_line = " | ".join(
        headers[index].ljust(widths[index]) for index in range(len(headers))
    )
    separator = "-+-".join("-" * widths[index] for index in range(len(headers)))
    body = [
        " | ".join(row[index].ljust(widths[index]) for index in range(len(headers)))
        for row in table_rows
    ]
    return "\n".join([header_line, separator, *body])




def render_contact_delivery_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "(no rows)"

    headers = ["customer", "contact", "from", "to", "invoice_email"]
    table_rows = [
        [
            str(row.get("customer_id") or ""),
            str(row.get("title") or "").replace("Contact: ", "", 1),
            str(row.get("delivery_method") or ""),
            "Email",
            str(row.get("invoice_email") or ""),
        ]
        for row in rows
    ]
    widths = [
        max(len(headers[index]), *(len(line[index]) for line in table_rows))
        for index in range(len(headers))
    ]
    header_line = " | ".join(
        headers[index].ljust(widths[index]) for index in range(len(headers))
    )
    separator = "-+-".join("-" * widths[index] for index in range(len(headers)))
    body = [
        " | ".join(row[index].ljust(widths[index]) for index in range(len(headers)))
        for row in table_rows
    ]
    return "\n".join([header_line, separator, *body])




def duplicate_fingerprint(action: str, payload: dict[str, Any]) -> str:
    serial = json.dumps({"action": action, "payload": payload}, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()
