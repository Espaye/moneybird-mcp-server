"""Deterministic candidate matching for unprocessed bank mutations.

Moneybird's own web interface does this: it auto-links a transaction when it is
100% certain, and otherwise shows ranked suggestions under the transaction so the
user confirms with one click. Nothing in the API exposes those suggestions, so a
model driving this server otherwise has to reconstruct them by hand — pulling the
debtors report, the open-invoice list and the counterparty's history, then
reasoning about amounts. That is the highest-volume bookkeeping task there is, and
doing it by inference is both the slowest path and the least reliable one.

This module does the join instead, and does it by arithmetic and string equality
rather than by judgement:

* **Direction decides the search space.** Money in can only settle a sales
  invoice; money out can only settle a purchase invoice or receipt.
* **Every candidate carries its evidence**, so a caller can show *why* something
  matched and a reviewer can disagree with a specific reason.
* **Confidence is a tier, not a score.** A number invites false precision; the
  tier names exactly which rules fired.
* **Ambiguity is reported, never resolved.** Two open invoices for the same
  amount is the single most common way an automated match goes wrong, so that
  case is flagged rather than broken by an arbitrary tie-break.

This module is advisory and read-only. It proposes nothing to Moneybird: linking
still goes through ``prepare_link_bank_mutation_booking`` and explicit approval.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from itertools import combinations
from typing import Any

from .formatting import money_decimal

# How far back an invoice may be dated relative to the mutation and still be a
# plausible settlement. Generous on purpose: overdue invoices are the norm, and
# date only ever *supports* a match here, it never creates one on its own.
MAX_INVOICE_AGE_DAYS = 400

# An invoice dated meaningfully after the payment is suspicious rather than
# impossible (prepayments happen), so it is allowed but never counted as support.
MAX_FUTURE_INVOICE_DAYS = 5

_TOKEN = re.compile(r"[A-Za-z0-9]{3,}")
_IBAN_NOISE = re.compile(r"[^A-Za-z0-9]")

CONFIDENCE_EXACT = "exact"
CONFIDENCE_STRONG = "strong"
CONFIDENCE_POSSIBLE = "possible"

MAX_GROUP_SIZE = 10
MAX_GROUP_FUTURE_INVOICE_DAYS = 45


def _decimal(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return money_decimal(value)
    except (InvalidOperation, ArithmeticError, ValueError, TypeError):
        return None


def _parse_date(value: Any) -> date | None:
    text = str(value or "").strip()[:10]
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return None


def normalize_iban(value: Any) -> str:
    return _IBAN_NOISE.sub("", str(value or "")).upper()


def _tokens(*values: Any) -> set[str]:
    joined = " ".join(str(value) for value in values if value)
    return {token.casefold() for token in _TOKEN.findall(joined)}


def mutation_text(mutation: dict[str, Any]) -> str:
    """Every free-text field a counterparty can put a reference into."""
    sepa = mutation.get("sepa_fields") or {}
    return " ".join(
        str(part)
        for part in (
            mutation.get("message"),
            mutation.get("contra_account_name"),
            sepa.get("remi") if isinstance(sepa, dict) else "",
            sepa.get("eref") if isinstance(sepa, dict) else "",
        )
        if part
    )


def _reference_hit(haystack: str, *references: Any) -> str:
    """The first invoice reference that literally occurs in the mutation text.

    Compared on alphanumerics only, so ``2026-014`` still matches ``2026014`` in a
    bank description that stripped the separator. Short references are skipped:
    a two-character "12" would match almost any statement line by accident.
    """
    normalized = _IBAN_NOISE.sub("", haystack).casefold()
    for reference in references:
        candidate = _IBAN_NOISE.sub("", str(reference or "")).casefold()
        if len(candidate) >= 4 and candidate in normalized:
            return str(reference)
    return ""


def _contact_ibans(record: dict[str, Any]) -> set[str]:
    contact = record.get("contact") or {}
    values = {
        normalize_iban(contact.get("sepa_iban")),
        normalize_iban(contact.get("bank_account")),
        normalize_iban(record.get("sepa_iban")),
    }
    return {value for value in values if value}


def _contact_name(record: dict[str, Any]) -> str:
    contact = record.get("contact") or {}
    return " ".join(
        str(part)
        for part in (
            contact.get("company_name"),
            contact.get("firstname"),
            contact.get("lastname"),
        )
        if part
    )


def open_amount(record: dict[str, Any]) -> Decimal | None:
    """What is still owed on an invoice or document."""
    unpaid = _decimal(record.get("total_unpaid"))
    if unpaid is not None:
        return unpaid
    total = _decimal(record.get("total_price_incl_tax"))
    if total is None:
        return None
    paid = sum(
        (_decimal(payment.get("price")) or Decimal("0"))
        for payment in record.get("payments") or []
        if isinstance(payment, dict)
    )
    return total - paid


def score_candidate(
    mutation: dict[str, Any],
    record: dict[str, Any],
    *,
    booking_type: str,
    kind: str = "",
) -> dict[str, Any] | None:
    """Evidence-bearing candidate for one (mutation, invoice) pair, or None.

    None means no rule fired at all. A candidate with no evidence is noise, and
    returning it would push the judgement back onto the model — which is exactly
    what this module exists to avoid.
    """
    mutation_amount = _decimal(mutation.get("amount_open"))
    if mutation_amount is None:
        mutation_amount = _decimal(mutation.get("amount"))
    if mutation_amount is None:
        return None
    magnitude = abs(mutation_amount)
    outstanding = open_amount(record)
    if outstanding is None:
        return None
    outstanding = abs(outstanding)

    evidence: list[str] = []
    text = mutation_text(mutation)

    reference = _reference_hit(
        text,
        record.get("invoice_id"),
        record.get("reference"),
        record.get("entry_number"),
    )
    if reference:
        evidence.append(f"reference '{reference}' appears in the bank description")

    amount_exact = magnitude == outstanding and magnitude > 0
    if amount_exact:
        evidence.append(f"amount matches the open balance exactly ({outstanding})")

    contra_iban = normalize_iban(mutation.get("contra_account_number"))
    iban_match = bool(contra_iban) and contra_iban in _contact_ibans(record)
    if iban_match:
        evidence.append("counterparty IBAN matches the contact")

    name_overlap = _tokens(mutation.get("contra_account_name")) & _tokens(
        _contact_name(record)
    )
    if name_overlap and not iban_match:
        evidence.append(
            "counterparty name matches the contact "
            f"({', '.join(sorted(name_overlap)[:3])})"
        )

    if not evidence:
        return None

    mutation_date = _parse_date(mutation.get("date"))
    record_date = _parse_date(record.get("invoice_date") or record.get("date"))
    date_plausible = True
    if mutation_date and record_date:
        age = (mutation_date - record_date).days
        if age < -MAX_FUTURE_INVOICE_DAYS or age > MAX_INVOICE_AGE_DAYS:
            date_plausible = False
            evidence.append(
                f"note: invoice dated {record_date} is far from the payment "
                f"on {mutation_date}"
            )

    if reference and amount_exact:
        confidence = CONFIDENCE_EXACT
    elif reference or (amount_exact and (iban_match or name_overlap)):
        confidence = CONFIDENCE_STRONG
    else:
        confidence = CONFIDENCE_POSSIBLE
    if not date_plausible and confidence == CONFIDENCE_EXACT:
        confidence = CONFIDENCE_STRONG

    return {
        "booking_type": booking_type,
        "booking_id": str(record.get("id") or ""),
        "document_kind": kind,
        "title": (
            str(record.get("invoice_id") or record.get("reference") or "")
            or f"{booking_type} {record.get('id')}"
        ),
        "contact": _contact_name(record) or None,
        "date": str(record.get("invoice_date") or record.get("date") or ""),
        "open_amount": str(outstanding),
        "price": str(mutation_amount),
        "confidence": confidence,
        "amount_matches_exactly": amount_exact,
        "evidence": evidence,
    }


_CONFIDENCE_ORDER = {
    CONFIDENCE_EXACT: 0,
    CONFIDENCE_STRONG: 1,
    CONFIDENCE_POSSIBLE: 2,
}


def match_mutation(
    mutation: dict[str, Any],
    *,
    sales_invoices: list[dict[str, Any]],
    purchase_documents: list[tuple[str, dict[str, Any]]],
    max_candidates: int = 3,
) -> dict[str, Any]:
    """Rank candidates for one mutation and describe what is left unresolved."""
    amount = _decimal(mutation.get("amount_open"))
    if amount is None:
        amount = _decimal(mutation.get("amount"))

    candidates: list[dict[str, Any]] = []
    if amount is not None and amount > 0:
        direction = "incoming"
        for invoice in sales_invoices:
            scored = score_candidate(
                mutation, invoice, booking_type="SalesInvoice"
            )
            if scored:
                candidates.append(scored)
    elif amount is not None and amount < 0:
        direction = "outgoing"
        for kind, document in purchase_documents:
            scored = score_candidate(
                mutation, document, booking_type="Document", kind=kind
            )
            if scored:
                candidates.append(scored)
    else:
        direction = "zero_or_unknown"

    candidates.sort(
        key=lambda item: (
            _CONFIDENCE_ORDER[item["confidence"]],
            0 if item["amount_matches_exactly"] else 1,
            -len(item["evidence"]),
        )
    )

    result: dict[str, Any] = {
        "financial_mutation_id": str(mutation.get("id") or ""),
        "date": str(mutation.get("date") or ""),
        "amount": str(amount) if amount is not None else "",
        "direction": direction,
        "contra_account_name": mutation.get("contra_account_name"),
        "description": mutation_text(mutation)[:200],
        "candidates": candidates[:max_candidates],
    }

    top = candidates[0] if candidates else None
    if top is None:
        result["suggestion"] = "none"
        result["note"] = (
            "No open invoice or document matched on reference, amount, IBAN, or "
            "name. Book it to a ledger account instead (prepare_link_bank_mutation_"
            "booking with booking_type LedgerAccount), after checking how this "
            "counterparty was booked before."
        )
        return result

    # An equally-confident runner-up is the case that quietly produces a wrong
    # booking, so name it rather than presenting the first one as the answer.
    tied = [
        item
        for item in candidates[1:]
        if item["confidence"] == top["confidence"]
        and item["amount_matches_exactly"] == top["amount_matches_exactly"]
    ]
    if tied:
        result["suggestion"] = "ambiguous"
        result["note"] = (
            f"{len(tied) + 1} candidates match equally well. Ask which one before "
            "linking; do not pick by order."
        )
    else:
        result["suggestion"] = top["confidence"]
    return result


def _same_counterparty(mutation: dict[str, Any], record: dict[str, Any]) -> bool:
    contra_iban = normalize_iban(mutation.get("contra_account_number"))
    if contra_iban and contra_iban in _contact_ibans(record):
        return True
    return bool(
        _tokens(mutation.get("contra_account_name")) & _tokens(_contact_name(record))
    )


def _group_date_plausible(mutation: dict[str, Any], record: dict[str, Any]) -> bool:
    mutation_date = _parse_date(mutation.get("date"))
    record_date = _parse_date(record.get("invoice_date") or record.get("date"))
    if not mutation_date or not record_date:
        return True
    age = (mutation_date - record_date).days
    return -MAX_GROUP_FUTURE_INVOICE_DAYS <= age <= MAX_INVOICE_AGE_DAYS


def _exact_subsets(
    mutations: list[dict[str, Any]],
    target: Decimal,
) -> list[tuple[int, ...]]:
    """Return at most two exact multi-mutation subsets for ambiguity detection."""

    if len(mutations) > MAX_GROUP_SIZE:
        return []
    amounts = []
    for mutation in mutations:
        amount = _decimal(mutation.get("amount_open"))
        if amount is None:
            amount = _decimal(mutation.get("amount"))
        amounts.append(abs(amount or Decimal()))
    matches = []
    for size in range(2, len(mutations) + 1):
        for subset in combinations(range(len(mutations)), size):
            if sum((amounts[index] for index in subset), Decimal()) == target:
                matches.append(subset)
                if len(matches) == 2:
                    return matches
    return matches


def match_mutation_groups(
    mutations: list[dict[str, Any]],
    purchase_documents: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Find exact, counterparty-matched groups that settle one purchase invoice."""

    def is_open_outgoing(mutation: dict[str, Any]) -> bool:
        amount = _decimal(mutation.get("amount_open"))
        if amount is None:
            amount = _decimal(mutation.get("amount"))
        return str(mutation.get("state") or "unprocessed") == "unprocessed" and bool(
            amount is not None and amount < 0
        )

    outgoing = [mutation for mutation in mutations if is_open_outgoing(mutation)]

    proposals: list[dict[str, Any]] = []
    for kind, document in purchase_documents:
        if kind != "purchase_invoice":
            continue
        outstanding = open_amount(document)
        if outstanding is None or outstanding <= 0:
            continue
        compatible = [
            mutation
            for mutation in outgoing
            if _same_counterparty(mutation, document)
            and _group_date_plausible(mutation, document)
        ]
        subsets = _exact_subsets(compatible, outstanding)
        if not subsets:
            continue
        chosen = subsets[0]
        selected = [compatible[index] for index in chosen]
        mutation_ids = [str(item.get("id") or "") for item in selected]
        proposal = {
            "suggestion": "strong" if len(subsets) == 1 else "ambiguous",
            "booking_id": str(document.get("id") or ""),
            "open_amount": f"{outstanding:.2f}",
            "financial_mutation_ids": mutation_ids,
            "process_purchase_invoice": True,
        }
        if len(subsets) > 1:
            proposal["alternative_financial_mutation_ids"] = [
                str(compatible[index].get("id") or "") for index in subsets[1]
            ]
        proposals.append(proposal)

    mutation_use = Counter(
        mutation_id
        for proposal in proposals
        for mutation_id in proposal["financial_mutation_ids"]
    )
    for proposal in proposals:
        overlapping = [
            mutation_id
            for mutation_id in proposal["financial_mutation_ids"]
            if mutation_use[mutation_id] > 1
        ]
        if overlapping:
            proposal["suggestion"] = "ambiguous"
    return proposals
