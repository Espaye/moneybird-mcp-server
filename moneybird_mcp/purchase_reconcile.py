"""Build safe purchase-document reconciliation payloads.

Moneybird's *boekingsregels* (booking rules) auto-fill an incoming purchase
invoice, but they are not exposed by the API and they apply inconsistently: one
month a supplier's invoice arrives with the usual multi-line split, the next it
lands as a single catch-all line, in ``new`` state, sometimes with
``prices_are_incl_tax`` flipped. This module gives two building blocks that turn
the manual "compare six months and rebuild the lines by hand" chore into
repeatable operations:

* :func:`build_reconcile_purchase_invoice` — reproduces a known-good reference
  invoice's line structure onto the target invoice, scaling line prices to the
  target total so the document total stays fixed to the cent. The output feeds
  the guarded ``prepare_* -> *_from_approval`` write flow.
* :func:`build_explicit_purchase_invoice_reconcile` — validates an exact line
  allocation transcribed from the actual invoice attachment, without proportional
  scaling, and refuses any split that changes the total.

Neither function writes anything; the tool layer stages the write and only the
``*_from_approval`` tool executes it after explicit user confirmation.

Line, tax and total arithmetic is not here. It lives in
:mod:`moneybird_mcp.document_lines`, which both this module and any out-of-tree
tool package reach through the same functions, so there is one rounding
behaviour rather than one per caller.
"""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .config import MoneybirdError
from .document_lines import (
    CENT,
    details_attributes_for_lines,
    line_ledger_account_id,
    line_tax_rate_id,
    line_total_incl_tax,
    line_view,
    validate_explicit_document_lines,
)
from .formatting import money_decimal, normalize_document_kind
from .purchase_review import list_documents_for_contact

_DUTCH_MONTHS = [
    "januari", "februari", "maart", "april", "mei", "juni",
    "juli", "augustus", "september", "oktober", "november", "december",
]

_TEMPLATE_KINDS = {"purchase_invoice", "receipt"}

def dutch_month_label(date_str: Any) -> str:
    """Return a Dutch 'maand jaar' label for an ISO date, or '' if unparseable.

    ``'2026-07-19' -> 'juli 2026'``. Used to relabel copied line descriptions
    from the reference month to the target month.
    """
    text = str(date_str or "")
    parts = text.split("-")
    if len(parts) < 2:
        return ""
    try:
        year = int(parts[0])
        month = int(parts[1])
    except ValueError:
        return ""
    if not 1 <= month <= 12:
        return ""
    return f"{_DUTCH_MONTHS[month - 1]} {year}"


def _line_signature(detail: dict[str, Any]) -> tuple[str, str, str, str]:
    """Comparable (ledger, tax, price, description) tuple for a line."""
    price = money_decimal(detail.get("price"))
    return (
        line_ledger_account_id(detail),
        line_tax_rate_id(detail),
        f"{price:.2f}",
        str(detail.get("description") or "").strip(),
    )


def _document_signature(document: dict[str, Any]) -> tuple:
    details = document.get("details") or []
    return (
        bool(document.get("prices_are_incl_tax")),
        tuple(sorted(_line_signature(d) for d in details)),
    )


def _version_snapshot(document: dict[str, Any]) -> dict[str, str]:
    """Return optimistic-lock fields that survive JSON approval persistence."""
    snapshot: dict[str, str] = {}
    if document.get("version") not in (None, ""):
        snapshot["expected_version"] = str(document.get("version"))
    if document.get("updated_at"):
        snapshot["expected_updated_at"] = str(document.get("updated_at"))
    return snapshot


# --------------------------------------------------------------------------- #
# Building a reconcile (fix) payload from a reference invoice
# --------------------------------------------------------------------------- #

def _calculated_total_incl_tax(
    desired: list[dict[str, Any]],
    *,
    prices_are_incl_tax: bool,
    tax_rates: dict[str, dict[str, Any]],
) -> Decimal:
    return sum(
        (
            line_total_incl_tax(
                line,
                prices_are_incl_tax=prices_are_incl_tax,
                tax_rates=tax_rates,
            )
            for line in desired
        ),
        Decimal("0"),
    ).quantize(CENT, rounding=ROUND_HALF_UP)


def _rebalance_to_incl_total(
    desired: list[dict[str, Any]],
    target_total: Decimal,
    *,
    prices_are_incl_tax: bool,
    tax_rates: dict[str, dict[str, Any]],
) -> Decimal:
    """Nudge a raw line price while preserving its incl/excl-tax meaning.

    ``target_total`` is always incl tax, while Moneybird interprets each line's
    ``price`` according to ``prices_are_incl_tax``.  In excl-tax mode, therefore,
    the residual cannot be copied into the raw price field directly.
    """
    if not desired:
        return Decimal("0.00")
    calculated = _calculated_total_incl_tax(
        desired,
        prices_are_incl_tax=prices_are_incl_tax,
        tax_rates=tax_rates,
    )
    if calculated == target_total:
        return calculated

    # Scaling and cent-rounding normally leave only a tiny residual. Try the
    # nearest raw-cent adjustments on each line and accept only an exact gross
    # total. If the copied tax split cannot express the requested total, fail the
    # preview instead of staging a write whose total is known to be wrong.
    residual = target_total - calculated
    for index in sorted(
        range(len(desired)),
        key=lambda item: abs(desired[item]["price"]),
        reverse=True,
    ):
        original = desired[index]["price"]
        multiplier = Decimal("1")
        if not prices_are_incl_tax:
            tax_rate = tax_rates.get(line_tax_rate_id(desired[index]))
            if tax_rate is None:
                continue
            percentage = Decimal(str(tax_rate.get("percentage") or "0"))
            multiplier += percentage / Decimal("100")
        nearest = (residual / multiplier).quantize(CENT, rounding=ROUND_HALF_UP)
        for offset in range(-3, 4):
            adjustment = nearest + CENT * offset
            desired[index]["price"] = (original + adjustment).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )
            recalculated = _calculated_total_incl_tax(
                desired,
                prices_are_incl_tax=prices_are_incl_tax,
                tax_rates=tax_rates,
            )
            if recalculated == target_total:
                return recalculated
        desired[index]["price"] = original

    raise MoneybirdError(
        "The reference line/tax split cannot be rounded to the requested incl-tax "
        f"total {target_total:.2f}. Supply exact desired_lines from the invoice instead."
    )


def _pick_reference_document(
    client: Any,
    kind: str,
    target: dict[str, Any],
    *,
    scan_limit: int = 8,
) -> dict[str, Any]:
    """Auto-select the same supplier's most canonical prior invoice as template.

    Canonical = the invoice with the most detail lines (a fully split booking),
    breaking ties by the most recent date. Fetches full documents for the most
    recent candidates only, so the call count stays bounded.
    """
    contact = target.get("contact") or {}
    contact_id = str(contact.get("id") or "")
    target_id = str(target.get("id") or "")
    if not contact_id:
        raise MoneybirdError(
            "Target invoice has no contact, so a reference cannot be auto-selected. "
            "Pass reference_document_id explicitly."
        )

    listed, _scan = list_documents_for_contact(
        client,
        kind,
        contact_id=contact_id,
        limit=100,
    )
    same_contact = [
        item
        for item in listed
        if str(item.get("id") or "") != target_id
    ]
    if not same_contact:
        raise MoneybirdError(
            "No other invoices from this supplier were found to use as a template. "
            "Pass reference_document_id explicitly."
        )

    same_contact.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    candidates: list[dict[str, Any]] = []
    for item in same_contact[:scan_limit]:
        document = item if item.get("details") else client.get_document(kind, str(item.get("id")))
        candidates.append(document)

    def score(document: dict[str, Any]) -> tuple[int, str]:
        return (len(document.get("details") or []), str(document.get("date") or ""))

    best = max(candidates, key=score)
    if len(best.get("details") or []) == 0:
        raise MoneybirdError(
            "The selected reference invoice has no lines. Pass reference_document_id explicitly."
        )
    return best


def build_reconcile_purchase_invoice(
    client: Any,
    *,
    document_id: str,
    document_kind: str = "purchase_invoice",
    reference_document_id: str = "",
    target_total: str = "",
    relabel_period: bool = True,
) -> dict[str, Any]:
    """Build the payload + preview to reproduce a reference invoice's booking.

    Returns ``{"payload": ..., "preview": ...}``. The caller stages the write.
    Line prices are scaled by ``target_total / reference_total`` and rebalanced
    to hit the target total exactly, so the document total is preserved to the
    cent (the default target total is the invoice's own current total).
    """
    kind = normalize_document_kind(document_kind)
    if kind not in _TEMPLATE_KINDS:
        raise MoneybirdError(
            "Reconciliation supports purchase_invoice and receipt documents only."
        )

    document_id = str(document_id).strip()
    if not document_id:
        raise MoneybirdError("document_id is required.")

    target = client.get_document(kind, document_id)

    reference_document_id = str(reference_document_id).strip()
    if reference_document_id:
        if reference_document_id == document_id:
            raise MoneybirdError("reference_document_id must differ from document_id.")
        reference = client.get_document(kind, reference_document_id)
    else:
        reference = _pick_reference_document(client, kind, target)

    reference_details = reference.get("details") or []
    if not reference_details:
        raise MoneybirdError("The reference invoice has no lines to use as a template.")

    reference_total = money_decimal(reference.get("total_price_incl_tax"))
    if reference_total == 0:
        raise MoneybirdError(
            "The reference invoice total is zero, so line prices cannot be scaled."
        )

    provided_total = str(target_total).strip()
    current_total = money_decimal(target.get("total_price_incl_tax"))
    resolved_total = money_decimal(provided_total) if provided_total else current_total
    if resolved_total == 0:
        raise MoneybirdError(
            "The target total is zero. Pass target_total explicitly to reconcile this invoice."
        )

    factor = resolved_total / reference_total
    reference_label = dutch_month_label(reference.get("date"))
    target_label = dutch_month_label(target.get("date"))
    prices_are_incl_tax = bool(reference.get("prices_are_incl_tax"))
    tax_rates = {
        str(rate.get("id") or ""): rate for rate in client.list_tax_rates()
    }

    desired: list[dict[str, Any]] = []
    for detail in reference_details:
        price = money_decimal(detail.get("price"))
        scaled = (price * factor).quantize(CENT, rounding=ROUND_HALF_UP)
        description = str(detail.get("description") or "")
        if (
            relabel_period
            and reference_label
            and target_label
            and reference_label != target_label
        ):
            description = description.replace(reference_label, target_label)
        desired.append(
            {
                "description": description,
                "ledger_account_id": line_ledger_account_id(detail),
                "tax_rate_id": line_tax_rate_id(detail),
                "price": scaled,
            }
        )

    calculated_total = _rebalance_to_incl_total(
        desired,
        resolved_total,
        prices_are_incl_tax=prices_are_incl_tax,
        tax_rates=tax_rates,
    )
    details_attributes = details_attributes_for_lines(target.get("details") or [], desired)

    # Did anything actually change? Compare the resulting line set + tax flag.
    desired_document = {
        "prices_are_incl_tax": prices_are_incl_tax,
        "details": [
            {
                "ledger_account_id": line["ledger_account_id"],
                "tax_rate_id": line["tax_rate_id"],
                "price": line["price"],
                "description": line["description"],
            }
            for line in desired
        ],
    }
    already_consistent = _document_signature(target) == _document_signature(desired_document)
    scaled = factor != 1

    before_lines = [
        {
            "id": str(detail.get("id")),
            "description": str(detail.get("description") or ""),
            "price": f'{money_decimal(detail.get("price")):.2f}',
            "ledger_account_id": line_ledger_account_id(detail),
            "tax_rate_id": line_tax_rate_id(detail),
        }
        for detail in (target.get("details") or [])
    ]
    after_lines = [
        {
            "description": line["description"],
            "price": f'{line["price"]:.2f}',
            "ledger_account_id": line["ledger_account_id"],
            "tax_rate_id": line["tax_rate_id"],
        }
        for line in desired
    ]

    warnings: list[str] = []
    if scaled:
        warnings.append(
            "Reference and target totals differ, so line prices were scaled "
            f"proportionally (factor {factor:.6f}). The stroom/gas or per-line split is "
            "an assumption copied from the reference; confirm it against the actual invoice PDF."
        )
    if bool(target.get("prices_are_incl_tax")) != prices_are_incl_tax:
        warnings.append(
            f"prices_are_incl_tax will change from {bool(target.get('prices_are_incl_tax'))} "
            f"to {prices_are_incl_tax} to match the reference."
        )
    if already_consistent:
        warnings.append(
            "The target invoice already matches the reference structure; no change is needed."
        )

    payload = {
        "document_kind": kind,
        "document_id": document_id,
        "details_attributes": details_attributes,
        "prices_are_incl_tax": prices_are_incl_tax,
        "expected_total_before": f"{current_total:.2f}",
        "expected_total_incl_tax": f"{calculated_total:.2f}",
        "expected_lines": line_view(desired),
        **_version_snapshot(target),
    }
    preview = {
        "document_id": document_id,
        "document_kind": kind,
        "reference_document_id": str(reference.get("id")),
        "reference_date": reference.get("date"),
        "target_date": target.get("date"),
        "contact": (target.get("contact") or {}).get("company_name")
        or (target.get("contact") or {}).get("name"),
        "reference_reference": reference.get("reference"),
        "target_reference": target.get("reference"),
        "target_state": target.get("state"),
        "target_version": target.get("version"),
        "total_before": f"{current_total:.2f}",
        "total_after": f"{calculated_total:.2f}",
        "total_unchanged": current_total == calculated_total,
        "prices_are_incl_tax_before": bool(target.get("prices_are_incl_tax")),
        "prices_are_incl_tax_after": prices_are_incl_tax,
        "line_count_before": len(before_lines),
        "line_count_after": len(after_lines),
        "before_lines": before_lines,
        "after_lines": after_lines,
        "scaled": scaled,
        "already_consistent": already_consistent,
        "warnings": warnings,
    }
    return {"payload": payload, "preview": preview}


def build_explicit_purchase_invoice_reconcile(
    client: Any,
    *,
    document_id: str,
    desired_lines: list[dict[str, Any]],
    document_kind: str = "purchase_invoice",
    prices_are_incl_tax: bool | None = None,
    source_note: str = "",
) -> dict[str, Any]:
    """Build an exact, total-preserving line allocation from an invoice source.

    This is the non-proportional companion to :func:`build_reconcile_purchase_invoice`.
    It is intended for amounts transcribed from the actual PDF attachment. The
    caller supplies the exact descriptions, prices, ledger ids, and tax-rate ids;
    the builder validates every id and refuses to stage a split whose calculated
    total differs from the current document total.
    """
    kind = normalize_document_kind(document_kind)
    if kind not in _TEMPLATE_KINDS:
        raise MoneybirdError(
            "Explicit reconciliation supports purchase_invoice and receipt documents only."
        )

    normalized_document_id = str(document_id or "").strip()
    if not normalized_document_id:
        raise MoneybirdError("document_id is required.")
    if not desired_lines:
        raise MoneybirdError("desired_lines must contain at least one exact invoice line.")

    target = client.get_document(kind, normalized_document_id)
    resolved_incl_flag = (
        bool(target.get("prices_are_incl_tax"))
        if prices_are_incl_tax is None
        else bool(prices_are_incl_tax)
    )
    current_total = money_decimal(target.get("total_price_incl_tax"))

    validated = validate_explicit_document_lines(
        client,
        document_kind=kind,
        lines=desired_lines,
        prices_are_incl_tax=resolved_incl_flag,
    )
    desired = validated.lines
    calculated_total_incl = validated.total_incl_tax
    if abs(calculated_total_incl - current_total) >= Decimal("0.005"):
        raise MoneybirdError(
            "The explicit line allocation would change the invoice total: "
            f"calculated {calculated_total_incl:.2f}, current {current_total:.2f}. "
            "Correct the PDF amounts or prices_are_incl_tax before preparing the write."
        )

    details_attributes = details_attributes_for_lines(target.get("details") or [], desired)
    desired_document = {
        "prices_are_incl_tax": resolved_incl_flag,
        "details": desired,
    }
    already_consistent = _document_signature(target) == _document_signature(desired_document)
    before_lines = [
        {
            "id": str(detail.get("id") or ""),
            "description": str(detail.get("description") or ""),
            "price": f'{money_decimal(detail.get("price")):.2f}',
            "ledger_account_id": line_ledger_account_id(detail),
            "tax_rate_id": line_tax_rate_id(detail),
        }
        for detail in (target.get("details") or [])
    ]
    after_lines = line_view(desired)

    warnings = [
        "Exact line amounts were supplied explicitly; confirm them against the invoice attachment."
    ]
    if bool(target.get("prices_are_incl_tax")) != resolved_incl_flag:
        warnings.append(
            f"prices_are_incl_tax will change from {bool(target.get('prices_are_incl_tax'))} "
            f"to {resolved_incl_flag}."
        )
    if already_consistent:
        warnings.append("The target invoice already matches the explicit line allocation.")

    payload = {
        "document_kind": kind,
        "document_id": normalized_document_id,
        "details_attributes": details_attributes,
        "prices_are_incl_tax": resolved_incl_flag,
        "expected_total_before": f"{current_total:.2f}",
        "expected_total_incl_tax": f"{current_total:.2f}",
        "expected_lines": after_lines,
        **_version_snapshot(target),
    }
    preview = {
        "mode": "explicit_lines",
        "source_note": str(source_note or "").strip(),
        "document_id": normalized_document_id,
        "document_kind": kind,
        "target_reference": target.get("reference"),
        "target_date": target.get("date"),
        "target_state": target.get("state"),
        "target_version": target.get("version"),
        "contact": (target.get("contact") or {}).get("company_name")
        or (target.get("contact") or {}).get("name"),
        "total_before": f"{current_total:.2f}",
        "total_after": f"{calculated_total_incl:.2f}",
        "total_unchanged": calculated_total_incl == current_total,
        "prices_are_incl_tax_before": bool(target.get("prices_are_incl_tax")),
        "prices_are_incl_tax_after": resolved_incl_flag,
        "line_count_before": len(before_lines),
        "line_count_after": len(after_lines),
        "before_lines": before_lines,
        "after_lines": after_lines,
        "already_consistent": already_consistent,
        "warnings": warnings,
    }
    return {"payload": payload, "preview": preview}
