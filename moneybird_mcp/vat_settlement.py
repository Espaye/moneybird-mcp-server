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
    # Each side is judged on its own. A net of zero from two opposite unexplained
    # halves is a coincidence, not a match.
    is_anomaly = unexplained_payable != ZERO or unexplained_deductible != ZERO

    if is_anomaly:
        explanation = (
            "Gross movements do not reconcile with the reported rubrieken: "
            f"payable differs by {unexplained_payable} and deductible by "
            f"{unexplained_deductible} beyond any offsetting reverse-charge amount "
            f"({offsetting}). A difference on either side on its own points at a "
            "missing, duplicated or misfiled VAT mutation; investigate it before "
            "settling, because a settlement would absorb it into the rounding line."
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
        "is_anomaly": is_anomaly,
        "explanation": explanation,
    }


def resolve_vat_accounts(
    ledger_accounts: list[dict[str, Any]],
    *,
    overrides: dict[str, str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Find the four settlement accounts by explicit id, else by conventional name.

    Raises with the available candidates rather than guessing, so an assistant
    working in an unfamiliar administration can ask instead of inventing a target.
    """

    overrides = {role: str(value or "").strip() for role, value in (overrides or {}).items()}
    by_id = {str(item.get("id")): item for item in ledger_accounts}
    resolved: dict[str, dict[str, Any]] = {}
    for role, default_name in DEFAULT_ACCOUNT_NAMES.items():
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
            available = ", ".join(
                sorted(
                    f"{item.get('account_id') or '?'} {item.get('name')}"
                    for item in ledger_accounts
                    if str(item.get("account_type") or "")
                    in {"current_liabilities", "current_assets", "expenses", "other_income_expenses"}
                )
            )
            raise MoneybirdError(
                f"Could not identify the {ACCOUNT_ROLE_LABELS[role]} account: found "
                f"{len(candidates)} ledger accounts named '{default_name}'. Pass an "
                f"explicit id for '{role}'. Candidates in this administration: {available}"
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
        (accounts["rounding"], -rounding_difference, "Afrondingsvoordeel aangifte"),
    ]
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
    journal_date: str = "",
    period_locked_until: str = "",
    period_end: str = "",
    comparison: dict[str, Any] | None = None,
    declared_amount_check: dict[str, Any] | None = None,
    allow_unexplained_difference: bool = False,
    allow_date_outside_period: bool = False,
) -> dict[str, Any]:
    """Check the period is not already settled, not locked, and actually settleable.

    The gross balances are checked separately from the net position on purpose: a
    period whose net happens to be zero can still carry offsetting gross movements
    that need clearing, and refusing it on the net alone would leave them stranded.

    Everything that could let a wrong figure through as "rounding" is refused here
    rather than surfaced in a preview, because the journal balances by construction
    and its post-write verifier cannot tell a mistake from an intended amount.
    """

    payable = movements[str(accounts["payable"]["id"])]
    receivable = movements[str(accounts["receivable"]["id"])]
    reference_matches = [
        journal
        for journal in existing_journals
        if str(journal.get("reference") or "").casefold() == reference.casefold()
    ]
    blocking: list[str] = []
    if reference_matches:
        blocking.append(
            f"A general journal document with reference '{reference}' already "
            f"exists ({len(reference_matches)} match(es)); this period looks settled."
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
            "The gross ledger movements do not reconcile with the reported "
            f"rubrieken: {comparison.get('explanation')} Settling now would absorb "
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
