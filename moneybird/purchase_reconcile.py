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
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import MoneybirdError
from .formatting import money_decimal, normalize_document_kind
from .purchase_review import list_documents_for_contact

CENT = Decimal("0.01")

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


def _line_ledger(detail: dict[str, Any]) -> str:
    return str(detail.get("ledger_account_id") or "")


def _line_tax(detail: dict[str, Any]) -> str:
    return str(detail.get("tax_rate_id") or "")


def _line_signature(detail: dict[str, Any]) -> tuple[str, str, str, str]:
    """Comparable (ledger, tax, price, description) tuple for a line."""
    price = money_decimal(detail.get("price"))
    return (
        _line_ledger(detail),
        _line_tax(detail),
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


def _expected_lines(desired: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "description": str(line.get("description") or ""),
            "price": f'{money_decimal(line.get("price")):.2f}',
            "ledger_account_id": _line_ledger(line),
            "tax_rate_id": _line_tax(line),
        }
        for line in desired
    ]


# --------------------------------------------------------------------------- #
# Building a reconcile (fix) payload from a reference invoice
# --------------------------------------------------------------------------- #

def _rebalance(desired: list[dict[str, Any]], target_total: Decimal) -> None:
    """Nudge the largest line so the prices sum exactly to ``target_total``."""
    if not desired:
        return
    total = sum((line["price"] for line in desired), Decimal("0"))
    residual = (target_total - total).quantize(CENT, rounding=ROUND_HALF_UP)
    if residual == 0:
        return
    biggest = max(range(len(desired)), key=lambda i: abs(desired[i]["price"]))
    desired[biggest]["price"] = (desired[biggest]["price"] + residual).quantize(
        CENT, rounding=ROUND_HALF_UP
    )


def _map_lines(
    current: list[dict[str, Any]],
    desired: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Turn a desired line set into details_attributes ops against current lines.

    Reuses an existing line (by matching ledger + tax) for each desired line to
    keep detail identity stable, appends the rest as new lines, and marks any
    leftover current line for deletion via ``_destroy``.
    """
    ops: list[dict[str, Any]] = []
    used = [False] * len(current)

    for want in desired:
        matched = None
        for index, line in enumerate(current):
            if used[index]:
                continue
            if _line_ledger(line) == want["ledger_account_id"] and _line_tax(line) == want["tax_rate_id"]:
                matched = index
                break
        price_text = f'{want["price"]:.2f}'
        if matched is not None:
            used[matched] = True
            ops.append(
                {
                    "id": str(current[matched].get("id")),
                    "description": want["description"],
                    "price": price_text,
                    "amount": "1",
                }
            )
        else:
            ops.append(
                {
                    "description": want["description"],
                    "price": price_text,
                    "amount": "1",
                    "ledger_account_id": want["ledger_account_id"],
                    "tax_rate_id": want["tax_rate_id"],
                }
            )

    for index, line in enumerate(current):
        if not used[index]:
            ops.append({"id": str(line.get("id")), "_destroy": "true"})

    return ops


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
                "ledger_account_id": _line_ledger(detail),
                "tax_rate_id": _line_tax(detail),
                "price": scaled,
            }
        )

    _rebalance(desired, resolved_total)

    prices_are_incl_tax = bool(reference.get("prices_are_incl_tax"))
    details_attributes = _map_lines(target.get("details") or [], desired)

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
            "ledger_account_id": _line_ledger(detail),
            "tax_rate_id": _line_tax(detail),
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
        "expected_total_incl_tax": f"{resolved_total:.2f}",
        "expected_lines": _expected_lines(desired),
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
        "total_after": f"{resolved_total:.2f}",
        "total_unchanged": current_total == resolved_total,
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

    ledger_accounts = {
        str(account.get("id")): account for account in client.list_ledger_accounts()
    }
    tax_rates = {str(rate.get("id")): rate for rate in client.list_tax_rates()}

    desired: list[dict[str, Any]] = []
    calculated_total_incl = Decimal("0.00")
    for index, raw_line in enumerate(desired_lines, start=1):
        description = str(raw_line.get("description") or "").strip()
        if not description:
            raise MoneybirdError(f"desired_lines[{index}] requires a description.")
        if raw_line.get("price") in (None, ""):
            raise MoneybirdError(f"desired_lines[{index}] requires a price.")

        amount = str(raw_line.get("amount") or "1").strip()
        if amount not in {"1", "1.0", "1.00", "1 x"}:
            raise MoneybirdError(
                f"desired_lines[{index}] amount must be 1; split the PDF into one "
                "explicit total per desired line."
            )
        price = money_decimal(raw_line.get("price"))
        ledger_id = str(raw_line.get("ledger_account_id") or "").strip()
        tax_id = str(raw_line.get("tax_rate_id") or "").strip()

        ledger = ledger_accounts.get(ledger_id)
        if ledger is None:
            raise MoneybirdError(
                f"desired_lines[{index}] ledger_account_id {ledger_id or '(empty)'} "
                "does not exist."
            )
        if ledger.get("active") is False:
            raise MoneybirdError(
                f"desired_lines[{index}] ledger account {ledger_id} is inactive."
            )
        allowed_types = set(ledger.get("allowed_document_types") or [])
        ledger_document_type = "purchase_invoice" if kind == "receipt" else kind
        if allowed_types and ledger_document_type not in allowed_types:
            raise MoneybirdError(
                f"desired_lines[{index}] ledger account {ledger_id} does not allow {kind}."
            )

        tax_rate = tax_rates.get(tax_id)
        if tax_rate is None:
            raise MoneybirdError(
                f"desired_lines[{index}] tax_rate_id {tax_id or '(empty)'} does not exist."
            )
        if tax_rate.get("active") is False:
            raise MoneybirdError(
                f"desired_lines[{index}] tax rate {tax_id} is inactive."
            )
        tax_type = str(tax_rate.get("tax_rate_type") or "")
        if tax_type and tax_type not in {kind, "purchase_invoice"}:
            raise MoneybirdError(
                f"desired_lines[{index}] tax rate {tax_id} is for {tax_type}, not {kind}."
            )

        desired.append(
            {
                "description": description,
                "price": price,
                "ledger_account_id": ledger_id,
                "tax_rate_id": tax_id,
            }
        )

        line_total_incl = price
        if not resolved_incl_flag:
            percentage = Decimal(str(tax_rate.get("percentage") or "0"))
            line_total_incl = (price * (Decimal("1") + percentage / Decimal("100"))).quantize(
                CENT,
                rounding=ROUND_HALF_UP,
            )
        calculated_total_incl += line_total_incl

    calculated_total_incl = calculated_total_incl.quantize(CENT, rounding=ROUND_HALF_UP)
    if abs(calculated_total_incl - current_total) >= Decimal("0.005"):
        raise MoneybirdError(
            "The explicit line allocation would change the invoice total: "
            f"calculated {calculated_total_incl:.2f}, current {current_total:.2f}. "
            "Correct the PDF amounts or prices_are_incl_tax before preparing the write."
        )

    details_attributes = _map_lines(target.get("details") or [], desired)
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
            "ledger_account_id": _line_ledger(detail),
            "tax_rate_id": _line_tax(detail),
        }
        for detail in (target.get("details") or [])
    ]
    after_lines = _expected_lines(desired)

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
