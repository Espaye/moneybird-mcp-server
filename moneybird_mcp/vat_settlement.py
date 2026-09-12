"""Btw-afwikkeling: clearing a filed VAT return period in the ledger.

Moneybird accrues output VAT on a payable account and input VAT on a receivable
account. Filing the return moves neither balance; only a settlement journal
(memoriaal) does. Without it both accounts keep accumulating across quarters and
the VAT position on the balance sheet stops meaning anything, even when every
individual payment was booked correctly.

The distinction that makes this non-trivial is **gross versus net**. Reverse-charge
VAT (``btw verlegd``) is booked as payable *and* deductible for the same amount, so
it inflates both gross movements while leaving the net position untouched. The
settlement journal has to clear the *gross* movements; only the *net* may be
compared with what was actually filed. Reporting an equal, offsetting excess on
both sides as a discrepancy -- or clearing only the amounts visible in the tax
report -- is the specific failure this module exists to prevent.

The filed amount is never derived here. A Dutch return is filed in whole euros and
may be rounded in the taxpayer's favour (output VAT down, input VAT up), so the
filed total is legitimately a few euros below the exact net. That gap grows with
the number of populated rubrieken, so no tolerance rule can reconstruct it: the
amount has to come from the return itself or from Moneybird's VAT overview.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, Iterable

from .config import MoneybirdError
from .formatting import money_decimal

ZERO = Decimal("0.00")

# Default names in Moneybird's Dutch chart of accounts. Every one of them can be
# overridden by id, because a customised administration may rename or split them.
DEFAULT_ACCOUNT_NAMES = {
    "payable": "Te betalen btw",
    "receivable": "Te vorderen btw",
    "settlement": "Betaalde en/of ontvangen btw",
    "rounding": "Afrondingsverschillen",
}

ACCOUNT_ROLE_LABELS = {
    "payable": "output VAT / te betalen btw",
    "receivable": "input VAT / te vorderen btw",
    "settlement": "settlement with the tax authority",
    "rounding": "rounding differences",
}

ACCOUNT_ROLE_TYPES = {
    "payable": {"current_liabilities"},
    "receivable": {"current_assets", "current_liabilities"},
    "settlement": {"current_assets", "current_liabilities"},
    "rounding": {"expenses", "other_income_expenses"},
}


@dataclass(frozen=True)
class LedgerMovement:
    """Debit and credit turnover of one ledger account over one period."""

    ledger_account_id: str
    debit: Decimal
    credit: Decimal

    @property
    def net_debit(self) -> Decimal:
        return self.debit - self.credit

    @property
    def net_credit(self) -> Decimal:
        return self.credit - self.debit


def _iter_report_rows(rows: Iterable[Any]) -> Iterable[dict[str, Any]]:
    """Yield every ledger row, descending into the report's optional children."""

    for row in rows or []:
        if not isinstance(row, dict):
            continue
        yield row
        children = row.get("children")
        if children:
            yield from _iter_report_rows(children)


def _side_totals(report: dict[str, Any], side: str) -> dict[str, Decimal]:
    section = report.get(side) or {}
    totals: dict[str, Decimal] = {}
    for row in _iter_report_rows(section.get("ledger_accounts") or []):
        account_id = str(row.get("ledger_account_id") or "")
        if not account_id:
            continue
        totals[account_id] = totals.get(account_id, ZERO) + money_decimal(
            row.get("value") or 0
        )
    return totals


def ledger_movements_from_report(
    report: dict[str, Any],
    account_ids: Iterable[str],
) -> dict[str, LedgerMovement]:
    """Extract per-account turnover from a ``general_ledger`` report response."""

    debits = _side_totals(report, "debit_sums")
    credits = _side_totals(report, "credit_sums")
    return {
        str(account_id): LedgerMovement(
            ledger_account_id=str(account_id),
            debit=debits.get(str(account_id), ZERO),
            credit=credits.get(str(account_id), ZERO),
        )
        for account_id in account_ids
    }


def _parse_range_day(text: str, label: str) -> date:
    digits = str(text or "").strip()
    if len(digits) != 8 or not digits.isdigit():
        raise MoneybirdError(
            f"{label} must be an 8-digit YYYYMMDD date, got '{text}'."
        )
    try:
        return date(int(digits[:4]), int(digits[4:6]), int(digits[6:8]))
    except ValueError as exc:
        raise MoneybirdError(f"{label} is not a valid date: {text}.") from exc


def month_periods(period: str) -> list[str]:
    """Split a whole-month ``YYYYMMDD..YYYYMMDD`` range into per-month periods.

    Moneybird's ``tax`` report refuses any period longer than a month ("Period
    cannot exceed 1 month"), so a quarter has to be fetched month by month and
    summed. A settlement period must therefore be stated as an explicit range that
    starts on the first and ends on the last day of a month; symbolic periods and
    partial months are refused rather than silently mis-summed.
    """

    text = str(period or "").strip()
    if ".." not in text:
        raise MoneybirdError(
            "A VAT settlement period must be an explicit range like "
            f"'20260401..20260630', got '{period}'."
        )
    start_text, end_text = text.split("..", 1)
    start = _parse_range_day(start_text, "Period start")
    end = _parse_range_day(end_text, "Period end")
    if start > end:
        raise MoneybirdError(f"Period start {start_text} is after its end {end_text}.")
    if start.day != 1:
        raise MoneybirdError(
            f"A VAT settlement period must start on the first of a month, got {start_text}."
        )
    if end.day != calendar.monthrange(end.year, end.month)[1]:
        raise MoneybirdError(
            f"A VAT settlement period must end on the last day of a month, got {end_text}."
        )
    periods: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        periods.append(f"{year}{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return periods


def period_end_date(period: str) -> str:
    """Return the ISO closing date of a whole-month settlement range.

    A settlement journal belongs at the end of the period it closes. Deriving that
    date instead of accepting a free one keeps the journal inside the window whose
    movements it clears -- booking it outside leaves those movements standing in
    the period report, where a second settlement could pick them up again.
    """

    month_periods(period)  # reuse the whole-month validation and its messages
    _, end_text = str(period).strip().split("..", 1)
    return _parse_range_day(end_text, "Period end").isoformat()


def find_vat_settlement_journals(
    existing_journals: Iterable[dict[str, Any]],
    *,
    accounts: dict[str, dict[str, Any]],
    period: str,
) -> list[dict[str, Any]]:
    """Find settlement-like journals by period and VAT-account participation.

    A free-text reference is not a durable identity for a VAT period. A normal
    settlement touches both VAT accounts, or one VAT account plus the tax-authority
    settlement account. Corrections that touch only one VAT account are deliberately
    not classified as settlements.
    """

    month_periods(period)
    start_text, end_text = str(period).strip().split("..", 1)
    start = _parse_range_day(start_text, "Period start")
    end = _parse_range_day(end_text, "Period end")
    role_ids = {
        role: str(account.get("id") or "")
        for role, account in accounts.items()
        if role in {"payable", "receivable", "settlement"}
    }
    matches: list[dict[str, Any]] = []
    for journal in existing_journals:
        journal_date = str(journal.get("date") or "").strip()
        try:
            journal_day = date.fromisoformat(journal_date)
        except ValueError:
            continue
        if not start <= journal_day <= end:
            continue
        entries = (
            journal.get("general_journal_document_entries")
            or journal.get("details")
            or journal.get("entries")
            or []
        )
        touched_ids = {
            str(entry.get("ledger_account_id") or "")
            for entry in entries
            if isinstance(entry, dict)
        }
        touched_roles = {
            role for role, account_id in role_ids.items() if account_id in touched_ids
        }
        touches_vat_pair = {"payable", "receivable"} <= touched_roles
        touches_vat_and_settlement = (
            "settlement" in touched_roles
            and bool({"payable", "receivable"} & touched_roles)
        )
        if not (touches_vat_pair or touches_vat_and_settlement):
            continue

        payable_restore = ZERO
        receivable_restore = ZERO
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            account_id = str(entry.get("ledger_account_id") or "")
            debit = money_decimal(entry.get("debit") or 0)
            credit = money_decimal(entry.get("credit") or 0)
            if account_id == role_ids.get("payable"):
                payable_restore += debit - credit
            if account_id == role_ids.get("receivable"):
                receivable_restore += credit - debit
        matches.append(
            {
                "id": str(journal.get("id") or ""),
                "reference": journal.get("reference"),
                "date": journal_date,
                "touched_roles": sorted(touched_roles),
                "payable_restore": str(payable_restore),
                "receivable_restore": str(receivable_restore),
            }
        )
    return matches


# ---------------------------------------------------------------------------
# Ledger-derived settlement evidence
#
# A general journal is not the only thing that clears a VAT period. Moneybird
# settles a btw-aangifte with its own document type, ``VatDocument``, which never
# appears in the general_journal_documents collection. Live-observed on a real
# administration, such a document posts exactly what a settlement journal posts:
# it credits the input-VAT account, debits the output-VAT account, debits the
# tax-authority settlement account for the net, and books the whole-euro
# difference to the rounding account. Detecting only general journals therefore
# reports a settled quarter as unsettled and invites a duplicate settlement.
#
# Three things stay strictly separate here:
#   * evidence that the VAT accounts were cleared (drives the write guard);
#   * reconstruction of the exact cleared amounts (may be withheld);
#   * filing status and payment/refund status (neither is knowable from this).
# Moneybird exposes no endpoint for a VAT return, so a document's own state is
# unobservable. That is deliberately not the question asked: a posted ledger line
# means the accounts *are* cleared, whatever the return's own status.
# ---------------------------------------------------------------------------

GROSS_VAT_ROLES = ("payable", "receivable")
SETTLEMENT_EVIDENCE_ROLES = ("payable", "receivable", "settlement")

# Moneybird's journal_entries report carries one signed ``amount`` per row and
# documents no debit/credit convention for it. Observed live the sign follows the
# ledger account's own natural side, which is not uniform: a debit shows positive
# on a current_assets account and negative on a current_liabilities one. Nothing
# in the API documents that, so it is never assumed. The mapping is proven per
# account and period against the general_ledger debit/credit totals, and derived
# amounts are withheld when the proof fails.
SIGN_POSITIVE_IS_DEBIT = "debit"
SIGN_POSITIVE_IS_CREDIT = "credit"

EVIDENCE_SOURCE_GENERAL_JOURNAL = "general_journal_document"
EVIDENCE_SOURCE_LEDGER_ENTRIES = "ledger_journal_entries"


@dataclass(frozen=True)
class SignMapping:
    """Which sign of ``journal_entries.amount`` is a debit, for one account+period."""

    ledger_account_id: str
    proven: bool
    positive_is: str
    reason: str

    def debit_credit(self, amount: Decimal) -> tuple[Decimal, Decimal]:
        """Split one signed row amount into (debit, credit)."""

        if not self.proven:
            raise MoneybirdError(
                "The journal-entry sign convention is unproven for ledger account "
                f"{self.ledger_account_id}; refusing to derive debit and credit."
            )
        magnitude = abs(amount)
        if (amount > ZERO) == (self.positive_is == SIGN_POSITIVE_IS_DEBIT):
            return magnitude, ZERO
        return ZERO, magnitude


def _row_amount(row: Any) -> Decimal:
    if not isinstance(row, dict):
        return ZERO
    return money_decimal(row.get("amount") or 0)


def prove_sign_mapping(
    rows: Iterable[dict[str, Any]],
    movement: LedgerMovement,
) -> SignMapping:
    """Prove the sign convention for one account by matching totals, never by assuming it.

    The positive and negative halves of the journal-entry rows must reproduce the
    general_ledger debit and credit totals for the same account and period. When
    both mappings fit -- symmetrical totals -- the direction is genuinely
    undecidable and is reported as unproven rather than picked.
    """

    rows = [row for row in rows if isinstance(row, dict)]
    account_id = str(movement.ledger_account_id)
    positives = sum(
        (amount for amount in map(_row_amount, rows) if amount > ZERO),
        start=ZERO,
    )
    negatives = sum(
        (-amount for amount in map(_row_amount, rows) if amount < ZERO),
        start=ZERO,
    )
    if not rows:
        if movement.debit == ZERO and movement.credit == ZERO:
            return SignMapping(
                account_id,
                True,
                SIGN_POSITIVE_IS_CREDIT,
                "no journal-entry rows and no general-ledger movement",
            )
        return SignMapping(
            account_id,
            False,
            "",
            "no journal-entry rows were returned while the general ledger shows "
            f"movement (debit {movement.debit}, credit {movement.credit})",
        )
    positive_is_credit = positives == movement.credit and negatives == movement.debit
    positive_is_debit = positives == movement.debit and negatives == movement.credit
    if positive_is_credit and positive_is_debit:
        return SignMapping(
            account_id,
            False,
            "",
            "the debit and credit totals are equal, so the sign convention cannot "
            "be told apart from the journal-entry rows",
        )
    if positive_is_credit:
        return SignMapping(
            account_id,
            True,
            SIGN_POSITIVE_IS_CREDIT,
            "the positive row total matches the general-ledger credit total",
        )
    if positive_is_debit:
        return SignMapping(
            account_id,
            True,
            SIGN_POSITIVE_IS_DEBIT,
            "the positive row total matches the general-ledger debit total",
        )
    return SignMapping(
        account_id,
        False,
        "",
        f"journal-entry totals (+{positives} / -{negatives}) match neither the "
        f"general-ledger debit {movement.debit} nor credit {movement.credit}",
    )


def prove_sign_mappings(
    *,
    rows_by_role: dict[str, list[dict[str, Any]]],
    movements_by_role: dict[str, LedgerMovement],
    account_types_by_role: dict[str, str],
    fully_scanned_roles: set[str] | None = None,
) -> dict[str, SignMapping]:
    """Prove each account's sign convention, falling back to its account type.

    A *fully settled* VAT account has equal debit and credit totals -- the accrual
    and its clearing cancel -- so its own rows cannot tell the two directions
    apart. That is precisely the case this detector exists for, so a per-account
    proof alone would withhold amounts almost every time it mattered.

    The direction observed live follows the account's natural side, which is a
    property of its ``account_type``: a debit shows positive on a current_assets
    account and negative on a current_liabilities one. So a direction proven
    unambiguously on one account of a type is carried to an ambiguous account of
    the same type -- and only when *every* unambiguous account of that type agrees.
    One disagreement leaves the whole type unproven rather than taking a majority,
    and the fallback records which account proved it so the inference is auditable.
    """

    # A role read for only some of the period's months cannot be matched against a
    # whole-period ledger total, so it is never proven directly: its rows are a
    # subset by construction, and a mismatch would say nothing about the sign. Such
    # a role goes straight to the account-type fallback.
    fully_scanned = (
        set(movements_by_role)
        if fully_scanned_roles is None
        else set(fully_scanned_roles)
    )
    direct = {
        role: (
            prove_sign_mapping(rows_by_role.get(role) or [], movement)
            if role in fully_scanned
            else SignMapping(
                str(movement.ledger_account_id),
                False,
                "",
                "only part of the period was read for this account, so its rows "
                "cannot be matched against the whole-period ledger total",
            )
        )
        for role, movement in movements_by_role.items()
    }
    by_type: dict[str, set[str]] = {}
    provers: dict[str, list[str]] = {}
    for role, mapping in direct.items():
        if not mapping.proven or not mapping.positive_is:
            continue
        movement = movements_by_role[role]
        # An account with no rows and no movement proves nothing about direction.
        if movement.debit == ZERO and movement.credit == ZERO:
            continue
        account_type = str(account_types_by_role.get(role) or "")
        if not account_type:
            continue
        by_type.setdefault(account_type, set()).add(mapping.positive_is)
        provers.setdefault(account_type, []).append(role)

    resolved: dict[str, SignMapping] = {}
    for role, mapping in direct.items():
        if mapping.proven:
            resolved[role] = mapping
            continue
        account_type = str(account_types_by_role.get(role) or "")
        agreed = by_type.get(account_type) or set()
        if len(agreed) == 1:
            positive_is = next(iter(agreed))
            resolved[role] = SignMapping(
                mapping.ledger_account_id,
                True,
                positive_is,
                "carried from "
                + ", ".join(sorted(provers.get(account_type, [])))
                + f" -- the same account_type '{account_type}' proved positive is a "
                f"{positive_is}, and every unambiguous account of that type agrees "
                f"({mapping.reason})",
            )
            continue
        if len(agreed) > 1:
            resolved[role] = SignMapping(
                mapping.ledger_account_id,
                False,
                "",
                f"accounts of type '{account_type}' disagree about which sign is a "
                f"debit, so no direction is adopted ({mapping.reason})",
            )
            continue
        resolved[role] = mapping
    return resolved


def find_ledger_settlement_occurrences(
    *,
    entry_rows_by_role: dict[str, list[dict[str, Any]]],
    accounts: dict[str, dict[str, Any]],
    sign_mappings: dict[str, SignMapping] | None = None,
    exclude_document_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Group journal-entry rows into settlement occurrences by source document.

    This is stricter than the rule ``find_vat_settlement_journals`` applies to
    general journals, and it has to be: that function only ever sees memoriaal
    documents, while this one sees *every* posted document, including ordinary
    purchase invoices. **Reverse-charge VAT** (btw verlegd) books input VAT and
    output VAT for the same amount, so every reverse-charge invoice touches both
    gross VAT accounts. Treating "touches both gross accounts" as a settlement
    therefore flags every such invoice -- verified against a live administration
    where it produced 7 false settlements in one quarter and 13 in the next, which
    would block legitimate settlements in any administration that imports goods or
    buys EU services.

    So the sign-free gate is the **tax-authority settlement account**: a settlement
    moves the net there, and a reverse-charge invoice never touches it. A document
    touching the settlement account *alone* is still not a settlement -- ``VatDocument``
    is also a documented ``link_booking`` booking_type, so a bank payment allocated
    to a VAT return legitimately appears there while clearing nothing. Corrections
    touching a single VAT account stay excluded as before.

    Where the amounts are provable, a second rule catches a settlement that routed
    no net to the settlement account: both gross accounts moved in the *clearing*
    direction (payable debited, receivable credited) rather than the accrual
    direction a reverse-charge invoice uses. That refinement only ever adds
    detections, and only on proven amounts, so an unprovable sign can never turn a
    reverse-charge invoice into a settlement.
    """

    sign_mappings = sign_mappings or {}
    role_ids = {
        role: str(accounts[role]["id"])
        for role in SETTLEMENT_EVIDENCE_ROLES
        if role in accounts and (accounts[role] or {}).get("id") is not None
    }
    excluded = {str(item) for item in exclude_document_ids}
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    for role, rows in (entry_rows_by_role or {}).items():
        if role not in role_ids:
            continue
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            document_id = str(row.get("document_id") or "")
            if not document_id:
                continue
            key = (str(row.get("document_type") or ""), document_id)
            slot = grouped.setdefault(
                key,
                {
                    "document_type": key[0],
                    "document_id": document_id,
                    "dates": set(),
                    "roles": set(),
                    "rows_by_role": {},
                },
            )
            slot["roles"].add(role)
            date_text = str(row.get("date") or "")
            if date_text:
                slot["dates"].add(date_text)
            slot["rows_by_role"].setdefault(role, []).append(row)

    occurrences: list[dict[str, Any]] = []
    for (document_type, document_id), slot in sorted(grouped.items()):
        if document_id in excluded:
            continue
        roles = slot["roles"]
        # Sign-free gate: the net went to the tax authority's account.
        touches_vat_and_settlement = "settlement" in roles and bool(
            {"payable", "receivable"} & roles
        )
        touches_vat_pair = {"payable", "receivable"} <= roles

        unproven: list[str] = []
        restores: dict[str, Decimal] = {"payable": ZERO, "receivable": ZERO}
        settlement_amount = ZERO
        for role in SETTLEMENT_EVIDENCE_ROLES:
            role_rows = slot["rows_by_role"].get(role) or []
            if not role_rows:
                continue
            mapping = sign_mappings.get(role)
            if mapping is None or not mapping.proven:
                unproven.append(role)
                continue
            for row in role_rows:
                debit, credit = mapping.debit_credit(_row_amount(row))
                if role == "payable":
                    restores["payable"] += debit - credit
                elif role == "receivable":
                    restores["receivable"] += credit - debit
                else:
                    settlement_amount += debit - credit
        reconstructed = not unproven
        # A clearing moves both gross accounts back towards zero; a reverse-charge
        # accrual moves both away from it. Only a proven clearing direction can
        # promote a settlement-account-less document to an occurrence.
        clears_both_gross_accounts = (
            touches_vat_pair
            and reconstructed
            and restores["payable"] > ZERO
            and restores["receivable"] > ZERO
        )
        if not (touches_vat_and_settlement or clears_both_gross_accounts):
            continue
        occurrences.append(
            {
                "source": EVIDENCE_SOURCE_LEDGER_ENTRIES,
                "document_type": document_type,
                "document_id": document_id,
                "date": min(slot["dates"]) if slot["dates"] else "",
                "touched_roles": sorted(roles),
                "amounts_reconstructed": reconstructed,
                "payable_restore": str(restores["payable"]) if reconstructed else None,
                "receivable_restore": (
                    str(restores["receivable"]) if reconstructed else None
                ),
                "settlement_amount": str(settlement_amount) if reconstructed else None,
                "unproven_roles": sorted(unproven),
                "matched_rule": (
                    "gross_vat_account_plus_settlement_account"
                    if touches_vat_and_settlement
                    else "both_gross_accounts_in_the_clearing_direction"
                ),
            }
        )
    return occurrences


def find_undetermined_gross_pairs(
    *,
    entry_rows_by_role: dict[str, list[dict[str, Any]]],
    sign_mappings: dict[str, SignMapping] | None = None,
    exclude_document_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Documents touching both gross VAT accounts whose direction is unknown.

    A document on both gross accounts is either a reverse-charge accrual or a
    clearing, and only the debit/credit direction tells them apart. When that
    direction is unproven the two are genuinely indistinguishable, so such a
    document must make the period *inconclusive* rather than settleable: absence of
    a provable direction is not evidence that a second settlement is safe.

    With a proven direction this returns nothing, because the accrual and the
    clearing separate cleanly -- which is the normal case.
    """

    sign_mappings = sign_mappings or {}
    if all(
        (sign_mappings.get(role) is not None and sign_mappings[role].proven)
        for role in GROSS_VAT_ROLES
    ):
        return []
    excluded = {str(item) for item in exclude_document_ids}
    roles_by_document: dict[tuple[str, str], set[str]] = {}
    for role in GROSS_VAT_ROLES:
        for row in entry_rows_by_role.get(role) or []:
            if not isinstance(row, dict):
                continue
            document_id = str(row.get("document_id") or "")
            if not document_id or document_id in excluded:
                continue
            key = (str(row.get("document_type") or ""), document_id)
            roles_by_document.setdefault(key, set()).add(role)
    return [
        {"document_type": document_type, "document_id": document_id}
        for (document_type, document_id), roles in sorted(roles_by_document.items())
        if set(GROSS_VAT_ROLES) <= roles
    ]


def find_vat_rounding_adjustments(
    existing_journals: Iterable[dict[str, Any]],
    *,
    accounts: dict[str, dict[str, Any]],
    period: str,
    exclude_document_ids: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Find VAT-account movements deliberately offset against the rounding account.

    A Dutch return is filed in whole euros, so the ledger is often trued up to the
    declared figure with a small journal against the rounding account. That entry
    is correctly *not* a settlement -- it touches one VAT account -- but its effect
    sits in the gross movement with nothing to explain it, which surfaces as a
    phantom anomaly. Identifying it by name and amount is not the same as ignoring
    it: the amount is reported, and only the part it actually covers is treated as
    explained.

    A journal must touch the rounding account *and* at least one gross VAT account,
    so rounding postings unrelated to VAT are never picked up.
    """

    rounding = accounts.get("rounding") or {}
    rounding_id = str(rounding.get("id") or "")
    if not rounding_id:
        return []
    month_periods(period)
    start_text, end_text = str(period).strip().split("..", 1)
    start = _parse_range_day(start_text, "Period start")
    end = _parse_range_day(end_text, "Period end")
    role_ids = {
        role: str((accounts.get(role) or {}).get("id") or "")
        for role in GROSS_VAT_ROLES
    }
    excluded = {str(item) for item in exclude_document_ids}
    adjustments: list[dict[str, Any]] = []
    for journal in existing_journals or []:
        journal_id = str(journal.get("id") or "")
        if journal_id in excluded:
            continue
        try:
            journal_day = date.fromisoformat(str(journal.get("date") or "").strip())
        except ValueError:
            continue
        if not start <= journal_day <= end:
            continue
        entries = (
            journal.get("general_journal_document_entries")
            or journal.get("details")
            or journal.get("entries")
            or []
        )
        touched = {
            str(entry.get("ledger_account_id") or "")
            for entry in entries
            if isinstance(entry, dict)
        }
        if rounding_id not in touched:
            continue
        if not any(
            account_id and account_id in touched for account_id in role_ids.values()
        ):
            continue
        payable_adjustment = ZERO
        receivable_adjustment = ZERO
        rounding_amount = ZERO
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            account_id = str(entry.get("ledger_account_id") or "")
            debit = money_decimal(entry.get("debit") or 0)
            credit = money_decimal(entry.get("credit") or 0)
            if account_id == role_ids.get("payable"):
                payable_adjustment += credit - debit
            elif account_id == role_ids.get("receivable"):
                receivable_adjustment += debit - credit
            elif account_id == rounding_id:
                rounding_amount += debit - credit
        if payable_adjustment == ZERO and receivable_adjustment == ZERO:
            continue
        adjustments.append(
            {
                "source": EVIDENCE_SOURCE_GENERAL_JOURNAL,
                "document_id": journal_id,
                "reference": journal.get("reference"),
                "date": journal_day.isoformat(),
                "category": "vat_rounding_correction",
                "reason": (
                    "a general journal moves a VAT account against the rounding "
                    f"account '{rounding.get('name')}', which is how a whole-euro "
                    "declaration difference is recorded"
                ),
                "rounding_ledger_account_id": rounding_id,
                "rounding_amount": str(rounding_amount),
                "payable_adjustment": str(payable_adjustment),
                "receivable_adjustment": str(receivable_adjustment),
            }
        )
    return adjustments


def count_rubrieken(reported: dict[str, Any]) -> int:
    """Number of distinct rubrieken behind a reported total.

    Filing rounds each rubriek to whole euros, so the achievable rounding
    advantage is bounded by how many rubrieken were populated -- strictly less
    than one euro each. That derived bound is what makes an implausible declared
    amount detectable without inventing a fixed tolerance.
    """

    references = {
        str(row.get("report_reference") or "").strip()
        for row in reported.get("rows") or []
    }
    references.discard("")
    # Even a single-rubriek return has a payable and a deductible side to round.
    return max(len(references), 2)


def validate_declared_amount(
    *,
    declared_amount: Decimal,
    net_position: Decimal,
    rubriek_count: int,
) -> dict[str, Any]:
    """Check a filed amount against the ledger position before it is booked.

    The settlement journal balances by construction: whatever gap exists between
    the ledger position and the declared amount lands on the rounding account. A
    mistyped amount would therefore produce a perfectly balanced, fully verified
    journal that quietly writes the error off as rounding. These checks are what
    stop that.
    """

    declared_amount = money_decimal(declared_amount)
    net_position = money_decimal(net_position)
    difference = net_position - declared_amount
    findings: list[str] = []

    if declared_amount != declared_amount.to_integral_value():
        findings.append(
            f"The declared amount {declared_amount} is not a whole number of euros. "
            "A Dutch VAT return is filed in whole euros; pass the figure as filed."
        )

    # Each rounded rubriek can shift the total by strictly less than one euro.
    bound = Decimal(rubriek_count)
    if abs(difference) >= bound:
        findings.append(
            f"The declared amount {declared_amount} differs from the ledger position "
            f"{net_position} by {difference}, which exceeds what rounding {rubriek_count} "
            f"rubrieken to whole euros can explain (< {bound}). Re-check the amount "
            "against the filed return before settling."
        )

    # Rounding in the taxpayer's favour lowers what is owed and raises what is
    # reclaimed, so a favourable difference is non-negative in both directions.
    in_favour = difference >= ZERO
    return {
        "declared_amount": str(declared_amount),
        "net_position": str(net_position),
        "rounding_difference": str(difference),
        "rounding_in_taxpayers_favour": in_favour,
        "rubriek_count": rubriek_count,
        "plausible_bound": str(bound),
        "findings": findings,
        "acceptable": not findings,
    }


def reported_vat_totals(tax_reports: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Sum one or more ``tax`` reports into payable and deductible totals.

    This is the *reported* view. It is deliberately kept separate from the ledger
    movements so the two can be compared instead of conflated: a reverse-charge
    row can carry a zero tax amount here while still moving both ledger accounts.
    """

    payable = ZERO
    deductible = ZERO
    rows: list[dict[str, Any]] = []
    for tax_report in tax_reports:
        for row in (tax_report or {}).get("tax_rates") or []:
            tax = money_decimal(row.get("tax") or 0)
            kind = str(row.get("type") or "")
            if kind == "sales_invoice":
                payable += tax
            else:
                deductible += tax
            rows.append(
                {
                    "name": row.get("name"),
                    "report_reference": row.get("report_reference"),
                    "type": kind,
                    "tax": str(tax),
                }
            )
    return {
        "payable": payable,
        "deductible": deductible,
        "net": payable - deductible,
        "rows": rows,
    }


def compare_gross_to_reported(
    *,
    gross_payable: Decimal,
    gross_deductible: Decimal,
    reported_payable: Decimal,
    reported_deductible: Decimal,
    explained_payable: Decimal = ZERO,
    explained_deductible: Decimal = ZERO,
    explained_adjustments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Explain a gross/reported gap instead of flagging offsetting halves of it.

    Reverse-charge VAT raises both gross movements by the same amount. Such a pair
    nets to zero and is expected, so it is reported as an explanation rather than
    an anomaly. Only the part that does *not* offset is a real discrepancy.

    What an offsetting pair does **not** establish is period membership. It only
    shows that the reverse-charge amounts cancel in the net. Whether these
    movements belong to the period at all follows from the date range they were
    fetched with and from the dates of the underlying records -- never from the
    two excesses happening to be equal.

    A second, narrower class of difference is explainable: a movement the books
    deliberately made against the rounding account to true the ledger up to a
    whole-euro declaration. Those are passed in as ``explained_*`` and are
    subtracted *after* the reverse-charge offset, so the arithmetic stays
    ``discrepancy - explained = residual``. They are never silently absorbed: the
    caller supplies them itemised, they are echoed back, and anything they do not
    cover remains an anomaly for its residual amount.
    """

    payable_excess = gross_payable - reported_payable
    deductible_excess = gross_deductible - reported_deductible
    # Only a *positive* excess on both sides can be reverse-charge VAT: it is an
    # extra booking on each account. Two equally negative differences mean both
    # accounts are short of what was reported, which is a missing or misfiled
    # mutation and must never be netted away as if it explained itself.
    offsetting = min(payable_excess, deductible_excess, key=abs) if (
        payable_excess > ZERO and deductible_excess > ZERO
    ) else ZERO
    unexplained_payable = payable_excess - offsetting
    unexplained_deductible = deductible_excess - offsetting
    net_unexplained = unexplained_payable - unexplained_deductible
    adjustments = list(explained_adjustments or [])
    residual_payable = unexplained_payable - explained_payable
    residual_deductible = unexplained_deductible - explained_deductible
    net_residual = residual_payable - residual_deductible
    # Each side is judged on its own. A net of zero from two opposite unexplained
    # halves is a coincidence, not a match.
    is_anomaly = residual_payable != ZERO or residual_deductible != ZERO

    if is_anomaly:
        explanation = (
            "Gross movements do not reconcile with the reported rubrieken: "
            f"payable differs by {residual_payable} and deductible by "
            f"{residual_deductible} beyond any offsetting reverse-charge amount "
            f"({offsetting})"
            + (
                f" and {len(adjustments)} explained rounding adjustment(s) "
                f"(payable {explained_payable}, deductible {explained_deductible})"
                if adjustments
                else ""
            )
            + ". A difference on either side on its own points at a "
            "missing, duplicated or misfiled VAT mutation; investigate it before "
            "settling, because a settlement would absorb it into the rounding line."
        )
    elif adjustments:
        explanation = (
            f"The gross/reported difference is fully explained: {offsetting} of "
            "offsetting reverse-charge VAT plus "
            f"{len(adjustments)} rounding adjustment(s) already booked against the "
            f"rounding account (payable {explained_payable}, deductible "
            f"{explained_deductible}). Nothing is left unexplained."
        )
    elif offsetting > ZERO:
        explanation = (
            f"Gross movements exceed the reported rubrieken by {offsetting} on both "
            "the payable and the deductible side. That is the signature of "
            "reverse-charge VAT (btw verlegd), which is booked as payable and as "
            "deductible for the same amount. It raises both gross balances that the "
            "settlement journal must clear, and leaves the net position unchanged."
        )
    else:
        explanation = "Gross movements match the reported rubrieken on both sides."

    return {
        "gross_payable": str(gross_payable),
        "gross_deductible": str(gross_deductible),
        "reported_payable": str(reported_payable),
        "reported_deductible": str(reported_deductible),
        "payable_excess": str(payable_excess),
        "deductible_excess": str(deductible_excess),
        "offsetting_amount": str(offsetting),
        "offsetting_explained": offsetting > ZERO,
        "unexplained_payable": str(unexplained_payable),
        "unexplained_deductible": str(unexplained_deductible),
        "net_unexplained": str(net_unexplained),
        # discrepancy - explained = residual, itemised rather than netted away.
        "explained_payable_adjustment": str(explained_payable),
        "explained_deductible_adjustment": str(explained_deductible),
        "explained_adjustments": adjustments,
        "residual_payable": str(residual_payable),
        "residual_deductible": str(residual_deductible),
        "net_residual": str(net_residual),
        "is_anomaly": is_anomaly,
        "explanation": explanation,
    }


def resolve_vat_accounts(
    ledger_accounts: list[dict[str, Any]],
    *,
    overrides: dict[str, str] | None = None,
    roles: Iterable[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Find requested settlement accounts by explicit id or conventional name.

    Raises with a short list of plausible candidates rather than guessing, so an assistant
    working in an unfamiliar administration can ask instead of inventing a target.
    """

    overrides = {role: str(value or "").strip() for role, value in (overrides or {}).items()}
    by_id = {str(item.get("id")): item for item in ledger_accounts}
    resolved: dict[str, dict[str, Any]] = {}
    for role in roles or DEFAULT_ACCOUNT_NAMES:
        default_name = DEFAULT_ACCOUNT_NAMES[role]
        override = overrides.get(role, "")
        if override:
            match = by_id.get(override)
            if not match:
                raise MoneybirdError(
                    f"Unknown ledger_account_id {override} for the "
                    f"{ACCOUNT_ROLE_LABELS[role]} account."
                )
            resolved[role] = match
            continue
        candidates = [
            item
            for item in ledger_accounts
            if str(item.get("name") or "").casefold() == default_name.casefold()
        ]
        if len(candidates) != 1:
            compatible = [
                item
                for item in ledger_accounts
                if str(item.get("account_type") or "") in ACCOUNT_ROLE_TYPES[role]
            ]
            if role == "rounding":
                # A generic expenses account is type-compatible but not a
                # plausible place for a VAT rounding difference. Only suggest
                # names that actually signal a difference/rounding purpose.
                semantic_terms = ("afrond", "round", "koers", "verschil", "difference")
                compatible = [
                    item
                    for item in compatible
                    if any(
                        term in str(item.get("name") or "").casefold()
                        for term in semantic_terms
                    )
                ]
                compatible.sort(
                    key=lambda item: (
                        str(item.get("account_type") or "")
                        != "other_income_expenses",
                        str(item.get("account_id") or ""),
                        str(item.get("name") or "").casefold(),
                    )
                )
            else:
                compatible.sort(
                    key=lambda item: (
                        str(item.get("account_id") or ""),
                        str(item.get("name") or "").casefold(),
                    )
                )
            available = ", ".join(
                f"{item.get('account_id') or '?'} {item.get('name')}"
                for item in compatible[:3]
            ) or (
                "none with a difference/rounding-related name"
                if role == "rounding"
                else "none"
            )
            guidance = (
                "Pass rounding_ledger_account_id. If no suitable account exists, "
                "create one through prepare_create_ledger_account before preparing "
                "the settlement journal."
                if role == "rounding"
                else f"Pass {role}_ledger_account_id."
            )
            raise MoneybirdError(
                f"Could not identify the {ACCOUNT_ROLE_LABELS[role]} account: found "
                f"{len(candidates)} ledger accounts named '{default_name}'. {guidance} "
                f"Plausible candidates: {available}"
            )
        resolved[role] = candidates[0]
    return resolved


def _journal_entry(
    ledger_account_id: str,
    signed_debit: Decimal,
    description: str,
) -> dict[str, Any] | None:
    """Render a signed amount as a debit or credit line, dropping exact zeroes."""

    if signed_debit == ZERO:
        return None
    amount = abs(signed_debit)
    return {
        "ledger_account_id": str(ledger_account_id),
        "debit": str(amount) if signed_debit > ZERO else "0.00",
        "credit": str(amount) if signed_debit < ZERO else "0.00",
        "description": description,
    }


def build_vat_settlement_journal(
    *,
    accounts: dict[str, dict[str, Any]],
    payable_movement: Decimal,
    receivable_movement: Decimal,
    declared_amount: Decimal,
    description: str,
) -> dict[str, Any]:
    """Build the balanced journal that clears one filed VAT period.

    ``payable_movement`` is the net credit turnover on the payable account and
    ``receivable_movement`` the net debit turnover on the receivable account --
    both gross, so both include any reverse-charge amounts. ``declared_amount`` is
    what was actually filed and settled: positive when owed to the tax authority,
    negative for a refund. The remainder is the rounding advantage and is the only
    figure this function derives.
    """

    payable_movement = money_decimal(payable_movement)
    receivable_movement = money_decimal(receivable_movement)
    declared_amount = money_decimal(declared_amount)

    net_position = payable_movement - receivable_movement
    rounding_difference = net_position - declared_amount

    # Signed as debit-positive: clearing a credit balance means debiting it.
    signed = [
        (accounts["payable"], payable_movement, "Te betalen btw afwikkelen"),
        (accounts["receivable"], -receivable_movement, "Te vorderen btw afwikkelen"),
        (accounts["settlement"], -declared_amount, "Aangegeven en afgerekend bedrag"),
    ]
    if rounding_difference != ZERO:
        rounding_account = accounts.get("rounding")
        if rounding_account is None:
            raise MoneybirdError(
                "This settlement needs a non-zero rounding line. Pass "
                "rounding_ledger_account_id, or create a suitable account through "
                "prepare_create_ledger_account first."
            )
        signed.append(
            (
                rounding_account,
                -rounding_difference,
                "Afrondingsvoordeel aangifte",
            )
        )
    entries = [
        entry
        for account, amount, line_description in signed
        if (
            entry := _journal_entry(
                str(account.get("id")),
                amount,
                f"{description} - {line_description}" if description else line_description,
            )
        )
        is not None
    ]
    if len(entries) < 2:
        raise MoneybirdError(
            "A VAT settlement journal needs at least two non-zero lines; the period "
            "shows no VAT movement to clear."
        )

    total_debit = sum(
        (money_decimal(entry["debit"]) for entry in entries),
        ZERO,
    )
    total_credit = sum(
        (money_decimal(entry["credit"]) for entry in entries),
        ZERO,
    )
    if total_debit != total_credit:
        raise MoneybirdError(
            f"VAT settlement journal is not balanced: debit {total_debit} vs "
            f"credit {total_credit}."
        )

    return {
        "entries": entries,
        "total_debit": str(total_debit),
        "total_credit": str(total_credit),
        "gross_payable": str(payable_movement),
        "gross_receivable": str(receivable_movement),
        "net_position": str(net_position),
        "declared_amount": str(declared_amount),
        "rounding_difference": str(rounding_difference),
        "accounts": {
            role: {
                "id": str(account.get("id")),
                "name": account.get("name"),
                "account_id": account.get("account_id"),
            }
            for role, account in accounts.items()
        },
    }


def settlement_preflight(
    *,
    movements: dict[str, LedgerMovement],
    accounts: dict[str, dict[str, Any]],
    existing_journals: list[dict[str, Any]],
    reference: str,
    period: str = "",
    journal_date: str = "",
    period_locked_until: str = "",
    period_end: str = "",
    comparison: dict[str, Any] | None = None,
    declared_amount_check: dict[str, Any] | None = None,
    allow_unexplained_difference: bool = False,
    allow_date_outside_period: bool = False,
    ledger_settlement_occurrences: list[dict[str, Any]] | None = None,
    settlement_evidence_complete: bool = True,
    settlement_evidence_gaps: list[str] | None = None,
) -> dict[str, Any]:
    """Check the period is not already settled, not locked, and actually settleable.

    The gross balances are checked separately from the net position on purpose: a
    period whose net happens to be zero can still carry offsetting gross movements
    that need clearing, and refusing it on the net alone would leave them stranded.

    Everything that could let a wrong figure through as "rounding" is refused here
    rather than surfaced in a preview, because the journal balances by construction
    and its post-write verifier cannot tell a mistake from an intended amount.

    This guard is deliberately stricter than the reporting layer. It refuses on the
    *presence* of clearing evidence, never on the reconstructed amounts, so a period
    whose amounts could not be derived is still protected. And when the evidence
    itself could not be established -- the journal-entry scan came back empty for an
    account the general ledger says moved -- it refuses rather than assuming the
    period is clean, because that gap is exactly where a duplicate settlement hides.
    """

    payable = movements[str(accounts["payable"]["id"])]
    receivable = movements[str(accounts["receivable"]["id"])]
    reference_matches = [
        journal
        for journal in existing_journals
        if str(journal.get("reference") or "").casefold() == reference.casefold()
    ]
    period_settlement_matches = (
        find_vat_settlement_journals(
            existing_journals,
            accounts=accounts,
            period=period,
        )
        if period
        else []
    )
    ledger_occurrences = list(ledger_settlement_occurrences or [])
    evidence_gaps = [str(gap) for gap in (settlement_evidence_gaps or []) if gap]
    blocking: list[str] = []
    if period_settlement_matches:
        blocking.append(
            f"VAT period {period} already contains {len(period_settlement_matches)} "
            "settlement-like general journal(s) touching the VAT accounts. Changing "
            "the journal reference does not make the period safe to settle again."
        )
    elif ledger_occurrences:
        described = ", ".join(
            f"{item.get('document_type') or 'document'} {item.get('document_id')}"
            f" ({item.get('date')})"
            for item in ledger_occurrences
        )
        blocking.append(
            f"VAT period {period} has already been cleared in the ledger by "
            f"{len(ledger_occurrences)} posted document(s) that are not general "
            f"journals: {described}. Moneybird settles a btw-aangifte with its own "
            "VatDocument, which does not appear in the general-journal collection. "
            "Settling again would clear the same movements twice. This refusal does "
            "not depend on whether the exact cleared amounts could be reconstructed."
        )
    elif reference_matches:
        blocking.append(
            f"A general journal document with reference '{reference}' already "
            f"exists ({len(reference_matches)} match(es)); this period looks settled."
        )
    if not settlement_evidence_complete:
        blocking.append(
            "Whether this VAT period was already cleared could not be established: "
            + "; ".join(evidence_gaps or ["the journal-entry evidence is incomplete"])
            + ". Refusing to settle on unproven evidence, because an undetected "
            "existing settlement would be cleared twice."
        )
    if payable.net_credit == ZERO and receivable.net_debit == ZERO:
        blocking.append(
            "Both VAT accounts show zero gross movement for this period, so there "
            "is nothing to clear."
        )
    # Moneybird refuses to book on or before the administration's lock date. The
    # *period end* is checked too, not just the journal date: dating a journal
    # after the lock would otherwise settle a locked period from outside it.
    locked_until = str(period_locked_until or "").strip()
    journal_day = str(journal_date or "").strip()
    closing_day = str(period_end or "").strip()
    for label, day in (("journal", journal_day), ("period end", closing_day)):
        if locked_until and day and day <= locked_until:
            blocking.append(
                f"The administration is locked through {locked_until} and the "
                f"{label} falls on {day}; booking into a locked period is refused."
            )
            break

    # A journal dated outside its own period leaves that period's movements
    # standing in the period report, where a second settlement can pick them up.
    if (
        closing_day
        and journal_day
        and journal_day != closing_day
        and not allow_date_outside_period
    ):
        blocking.append(
            f"The journal is dated {journal_day} but the period closes on "
            f"{closing_day}. Settling from outside the period leaves its movements "
            "visible for a second settlement. Pass allow_date_outside_period to "
            "override deliberately."
        )

    if comparison and comparison.get("is_anomaly") and not allow_unexplained_difference:
        blocking.append(
            "The VAT comparison is anomalous. "
            f"{comparison.get('explanation')} Settling now would absorb "
            "that difference into the rounding line. Investigate first, or pass "
            "allow_unexplained_difference to record a deliberate exception."
        )

    if declared_amount_check and not declared_amount_check.get("acceptable", True):
        blocking.extend(declared_amount_check.get("findings", []))

    return {
        "gross_payable_movement": str(payable.net_credit),
        "gross_receivable_movement": str(receivable.net_debit),
        "existing_reference_matches": [
            {"id": journal.get("id"), "date": journal.get("date")}
            for journal in reference_matches
        ],
        "existing_period_settlement_matches": period_settlement_matches,
        "ledger_settlement_occurrences": ledger_occurrences,
        "settlement_evidence_complete": bool(settlement_evidence_complete),
        "settlement_evidence_gaps": evidence_gaps,
        "period_locked_until": locked_until,
        "period_end": closing_day,
        "journal_date": journal_day,
        "overrides": {
            "allow_unexplained_difference": bool(allow_unexplained_difference),
            "allow_date_outside_period": bool(allow_date_outside_period),
        },
        "blocking_findings": blocking,
        "clear_to_prepare": not blocking,
    }
