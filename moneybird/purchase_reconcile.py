"""Purchase-invoice reconciliation against a supplier's established booking pattern.

Moneybird's *boekingsregels* (booking rules) auto-fill an incoming purchase
invoice, but they are not exposed by the API and they apply inconsistently: one
month a supplier's invoice arrives with the usual multi-line split, the next it
lands as a single catch-all line, in ``new`` state, sometimes with
``prices_are_incl_tax`` flipped. This module gives two building blocks that turn
the manual "compare six months and rebuild the lines by hand" chore into
repeatable operations:

* :func:`scan_purchase_invoices_for_attention` — read-only detector that flags
  invoices which are still ``new`` or deviate from the same supplier's usual
  booking (fewer lines, different ledgers, or a different incl/excl-tax flag).
* :func:`build_reconcile_purchase_invoice` — reproduces a known-good reference
  invoice's line structure onto the target invoice, scaling line prices to the
  target total so the document total stays fixed to the cent. The output feeds
  the guarded ``prepare_* -> *_from_approval`` write flow.

Neither function writes anything; the tool layer stages the write and only the
``*_from_approval`` tool executes it after explicit user confirmation.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from .config import MoneybirdError
from .formatting import money_decimal, normalize_document_kind

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

    listed = client.list_documents(kind, limit=100)
    same_contact = [
        item
        for item in listed
        if str((item.get("contact") or {}).get("id") or "") == contact_id
        and str(item.get("id") or "") != target_id
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
        "expected_total_incl_tax": f"{resolved_total:.2f}",
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


# --------------------------------------------------------------------------- #
# Detecting invoices that need attention
# --------------------------------------------------------------------------- #

def _canonical_pattern(documents: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Modal line-count / ledger-set / incl-tax flag for a supplier's invoices.

    Uses the most common ('modal') values across the supplier's invoices as the
    expected booking. Returns ``None`` when there is not enough history (fewer
    than two invoices) to establish a pattern.
    """
    if len(documents) < 2:
        return None

    line_counts = Counter(len(doc.get("details") or []) for doc in documents)
    ledger_sets = Counter(
        frozenset(_line_ledger(d) for d in (doc.get("details") or []))
        for doc in documents
    )
    incl_flags = Counter(bool(doc.get("prices_are_incl_tax")) for doc in documents)

    modal_line_count = line_counts.most_common(1)[0][0]
    modal_ledgers = ledger_sets.most_common(1)[0][0]
    modal_incl = incl_flags.most_common(1)[0][0]

    # A canonical example invoice: one that matches the modal line count, most
    # recent first, to suggest as the reconcile reference.
    example = None
    for doc in sorted(documents, key=lambda d: str(d.get("date") or ""), reverse=True):
        if len(doc.get("details") or []) == modal_line_count:
            example = doc
            break

    return {
        "modal_line_count": modal_line_count,
        "modal_ledgers": modal_ledgers,
        "modal_prices_are_incl_tax": modal_incl,
        "example_document_id": str(example.get("id")) if example else "",
    }


def _needs_details(documents: list[dict[str, Any]]) -> bool:
    return any("details" not in doc for doc in documents)


def scan_purchase_invoices_for_attention(
    client: Any,
    *,
    kind: str = "purchase_invoice",
    period: str = "",
    limit: int = 100,
    contact_id: str = "",
) -> dict[str, Any]:
    """Flag purchase invoices that are still ``new`` or deviate from their supplier's pattern.

    Read-only. For each supplier with enough history it derives the modal booking
    (line count, ledger set, incl/excl-tax flag) and flags invoices that fall
    short, plus any invoice still in ``new`` state. Each flagged invoice carries a
    suggested reconcile reference (a canonical prior invoice from the same
    supplier).
    """
    normalized_kind = normalize_document_kind(kind)
    if normalized_kind not in _TEMPLATE_KINDS:
        raise MoneybirdError(
            "Attention scan supports purchase_invoice and receipt documents only."
        )

    documents = client.list_documents(normalized_kind, limit=limit, period=period)
    if contact_id:
        documents = [
            doc
            for doc in documents
            if str((doc.get("contact") or {}).get("id") or "") == str(contact_id)
        ]

    # Ensure each document carries its detail lines (the index endpoint usually
    # includes them; fall back to per-document fetches only when it does not).
    if _needs_details(documents):
        documents = [
            doc if "details" in doc else client.get_document(normalized_kind, str(doc.get("id")))
            for doc in documents
        ]

    by_contact: dict[str, list[dict[str, Any]]] = {}
    for doc in documents:
        cid = str((doc.get("contact") or {}).get("id") or "")
        by_contact.setdefault(cid, []).append(doc)

    patterns = {cid: _canonical_pattern(docs) for cid, docs in by_contact.items()}

    flagged: list[dict[str, Any]] = []
    for doc in documents:
        cid = str((doc.get("contact") or {}).get("id") or "")
        pattern = patterns.get(cid)
        details = doc.get("details") or []
        reasons: list[str] = []

        if str(doc.get("state") or "") == "new":
            reasons.append("state is 'new' (not booked yet)")

        if pattern is not None:
            if pattern["modal_line_count"] > 1 and len(details) < pattern["modal_line_count"]:
                reasons.append(
                    f"has {len(details)} line(s); this supplier usually has "
                    f"{pattern['modal_line_count']}"
                )
            ledgers = frozenset(_line_ledger(d) for d in details)
            if ledgers != pattern["modal_ledgers"] and pattern["modal_ledgers"]:
                missing = pattern["modal_ledgers"] - ledgers
                if missing:
                    reasons.append(
                        f"missing usual ledger account(s): {', '.join(sorted(missing))}"
                    )
            if bool(doc.get("prices_are_incl_tax")) != pattern["modal_prices_are_incl_tax"]:
                reasons.append(
                    f"prices_are_incl_tax={bool(doc.get('prices_are_incl_tax'))}, "
                    f"supplier usually {pattern['modal_prices_are_incl_tax']}"
                )

        if not reasons:
            continue

        suggestion = ""
        if pattern and pattern["example_document_id"] and pattern["example_document_id"] != str(doc.get("id")):
            suggestion = pattern["example_document_id"]

        flagged.append(
            {
                "document_id": str(doc.get("id")),
                "document_kind": normalized_kind,
                "date": doc.get("date"),
                "reference": doc.get("reference"),
                "contact": (doc.get("contact") or {}).get("company_name")
                or (doc.get("contact") or {}).get("name"),
                "state": doc.get("state"),
                "total_price_incl_tax": doc.get("total_price_incl_tax"),
                "line_count": len(details),
                "reasons": reasons,
                "suggested_reference_document_id": suggestion,
            }
        )

    flagged.sort(key=lambda item: str(item.get("date") or ""), reverse=True)
    return {
        "flagged": flagged,
        "count": len(flagged),
        "scanned": len(documents),
        "period": period,
    }
