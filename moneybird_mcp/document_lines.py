"""Validate and normalise an explicitly stated set of document lines.

A caller who has transcribed the amounts off a source document -- an invoice
PDF, a supplier's e-mail, a statement -- states the lines exactly, and something
has to turn that statement into a set the provider will accept: every ledger
account and tax rate proved to exist, to be active, and to be usable on a
document of this kind; every price parsed as exact decimal money; and the gross
total calculated the same way every time, because that total is what a write
contract later compares against.

This module is that step and nothing more. It does not fetch the document, does
not decide what the total ought to be, does not stage or execute anything, and
has no opinion about which workflow is asking. Those decisions differ per
capability; the arithmetic does not, and a second implementation of it is a
second rounding behaviour waiting to disagree with the first about a cent.

Money is parsed with :func:`~moneybird_mcp.formatting.money_decimal` and
quantised to :data:`CENT` with ``ROUND_HALF_UP``, per line and again on the sum.
Nothing here uses binary floating point.

The comparison helpers belong to the same question from the other end. A guarded
write states lines, and afterwards something has to prove the lines that arrived
are the lines that were stated -- which is the same field-by-field, price-as-text
reading of a line, and would be the same copy if it lived anywhere else.

Diagnostics name the caller's own field, so a refusal reads as a statement about
the input the caller was given rather than about this module's parameters.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from .config import MoneybirdError
from .formatting import money_decimal

#: Quantisation unit for money. Every total here is exact to the cent.
CENT = Decimal("0.01")

#: Quantities other than one are refused rather than multiplied out. An explicit
#: allocation states one total per line, so a quantity is either the identity or
#: a sign that the caller is describing something this step cannot verify.
_UNIT_AMOUNTS = {"1", "1.0", "1.00", "1 x"}


def line_ledger_account_id(line: dict[str, Any]) -> str:
    """The ledger account id on a document line, as a string, or ``""``."""
    return str(line.get("ledger_account_id") or "")


def line_tax_rate_id(line: dict[str, Any]) -> str:
    """The tax rate id on a document line, as a string, or ``""``."""
    return str(line.get("tax_rate_id") or "")


def line_total_incl_tax(
    line: dict[str, Any],
    *,
    prices_are_incl_tax: bool,
    tax_rates: dict[str, dict[str, Any]],
) -> Decimal:
    """One line's gross total, honouring what its ``price`` field means.

    Moneybird reads a line's ``price`` as gross or net according to the
    document's ``prices_are_incl_tax`` flag, so the flag has to be applied here
    rather than assumed by the caller.
    """
    price = money_decimal(line.get("price"))
    if prices_are_incl_tax:
        return price.quantize(CENT, rounding=ROUND_HALF_UP)
    tax_rate_id = line_tax_rate_id(line)
    tax_rate = tax_rates.get(tax_rate_id)
    if tax_rate is None:
        raise MoneybirdError(
            f"Reference line uses unknown tax_rate_id {tax_rate_id or '(empty)'}; "
            "cannot prove the incl-tax total."
        )
    percentage = Decimal(str(tax_rate.get("percentage") or "0"))
    return (price * (Decimal("1") + percentage / Decimal("100"))).quantize(
        CENT,
        rounding=ROUND_HALF_UP,
    )


def line_signature(line: dict[str, Any]) -> tuple[str, str, str, str]:
    """A comparable (ledger, tax, price, description) tuple for one line."""
    return (
        line_ledger_account_id(line),
        line_tax_rate_id(line),
        f'{money_decimal(line.get("price")):.2f}',
        str(line.get("description") or "").strip(),
    )


def line_signatures(lines: Any) -> list[tuple[str, str, str, str]]:
    """Every line's signature, sorted, so two line sets compare regardless of order.

    This is what a guarded write uses to prove the lines it asked for are the
    lines that arrived. Provider order is not stable and is not part of the
    promise; the fields here are, and the price is compared as fixed
    two-decimal text so two exact decimals that differ only in trailing zeros
    still compare equal.
    """
    return sorted(line_signature(line) for line in lines)


def booking_line_snapshot(lines: Any) -> list[dict[str, Any]]:
    """The booking fields of a line set that a save must leave alone.

    Wider than :func:`line_signatures` and used for a different question: not
    "are these the lines I asked for" but "did anything else move". So it keeps
    the provider's own identity and ordering fields, and orders by them, which
    makes two snapshots of the same document comparable across a save that
    reorders the rows.
    """
    return sorted(
        (
            {
                "id": str(line.get("id") or ""),
                "description": str(line.get("description") or ""),
                "price": f'{money_decimal(line.get("price")):.2f}',
                "amount": str(
                    line.get("amount_decimal") or line.get("amount") or "1"
                ),
                "ledger_account_id": line_ledger_account_id(line),
                "tax_rate_id": line_tax_rate_id(line),
                "project_id": str(line.get("project_id") or ""),
                "product_id": str(line.get("product_id") or ""),
                "period": str(line.get("period") or ""),
                "row_order": int(line.get("row_order") or 0),
            }
            for line in lines
        ),
        key=lambda line: (line["row_order"], line["id"]),
    )


def line_view(lines: Any) -> list[dict[str, str]]:
    """A stable, all-strings view of a line set, for previews and comparisons.

    Prices become fixed two-decimal text here, which is what makes two of these
    comparable: a preview an operator approved and a read-back that has to prove
    the same lines arrived.
    """
    return [
        {
            "description": str(line.get("description") or ""),
            "price": f'{money_decimal(line.get("price")):.2f}',
            "ledger_account_id": line_ledger_account_id(line),
            "tax_rate_id": line_tax_rate_id(line),
        }
        for line in lines
    ]


def details_attributes_for_lines(
    current: Any,
    desired: Any,
) -> list[dict[str, Any]]:
    """Turn a desired line set into ``details_attributes`` ops against current lines.

    Reuses an existing line (by matching ledger + tax) for each desired line to
    keep detail identity stable, appends the rest as new lines, and marks any
    leftover current line for deletion via ``_destroy``.
    """
    current = list(current)
    ops: list[dict[str, Any]] = []
    used = [False] * len(current)

    for want in desired:
        matched = None
        for index, line in enumerate(current):
            if used[index]:
                continue
            if (
                line_ledger_account_id(line) == want["ledger_account_id"]
                and line_tax_rate_id(line) == want["tax_rate_id"]
            ):
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


@dataclass(frozen=True)
class ExplicitDocumentLines:
    """A validated line set and the gross total it comes to.

    ``lines`` are normalised: a stripped description, an exact :class:`Decimal`
    price, and ledger/tax ids proved to exist. ``total_incl_tax`` is the sum of
    their gross totals, quantised to the cent. What the caller does with that
    total -- require it to match the document, require it to be positive, refuse
    it outright -- is the caller's rule, not this one's.
    """

    lines: tuple[dict[str, Any], ...]
    total_incl_tax: Decimal

    def view(self) -> list[dict[str, str]]:
        """The all-strings view of :attr:`lines`, for a preview or a read-back."""
        return line_view(self.lines)

    def details_attributes(self, current: Any = ()) -> list[dict[str, Any]]:
        """``details_attributes`` ops that turn ``current`` into :attr:`lines`."""
        return details_attributes_for_lines(current, self.lines)


def validate_explicit_document_lines(
    client: Any,
    *,
    document_kind: str,
    lines: Any,
    prices_are_incl_tax: bool,
    field_name: str = "desired_lines",
) -> ExplicitDocumentLines:
    """Validate caller-supplied document lines and total them.

    ``client`` is used for two reads: the ledger accounts and the tax rates of
    the current administration. Every line is then checked against both -- the
    account exists, is active, and accepts this kind of document; the tax rate
    exists, is active, and is for this kind of document -- before any arithmetic
    happens, so a refusal names the line and the reason rather than producing a
    total nobody should trust.

    ``document_kind`` is the provider's own document kind. A receipt is filed
    under purchase-invoice ledger and tax typing, which is Moneybird's rule and
    not this module's.

    ``field_name`` is the name the caller's own input goes by, and appears in
    every message.
    """
    lines = list(lines)
    if not lines:
        raise MoneybirdError(f"{field_name} must contain at least one exact invoice line.")

    ledger_accounts = {
        str(account.get("id")): account for account in client.list_ledger_accounts()
    }
    tax_rates = {str(rate.get("id")): rate for rate in client.list_tax_rates()}

    kind = str(document_kind)
    ledger_document_type = "purchase_invoice" if kind == "receipt" else kind

    validated: list[dict[str, Any]] = []
    total_incl_tax = Decimal("0.00")
    for index, raw_line in enumerate(lines, start=1):
        description = str(raw_line.get("description") or "").strip()
        if not description:
            raise MoneybirdError(f"{field_name}[{index}] requires a description.")
        if raw_line.get("price") in (None, ""):
            raise MoneybirdError(f"{field_name}[{index}] requires a price.")

        amount = str(raw_line.get("amount") or "1").strip()
        if amount not in _UNIT_AMOUNTS:
            raise MoneybirdError(
                f"{field_name}[{index}] amount must be 1; split the source into one "
                "explicit total per desired line."
            )
        price = money_decimal(raw_line.get("price"))
        ledger_id = str(raw_line.get("ledger_account_id") or "").strip()
        tax_id = str(raw_line.get("tax_rate_id") or "").strip()

        ledger = ledger_accounts.get(ledger_id)
        if ledger is None:
            raise MoneybirdError(
                f"{field_name}[{index}] ledger_account_id {ledger_id or '(empty)'} "
                "does not exist."
            )
        if ledger.get("active") is False:
            raise MoneybirdError(
                f"{field_name}[{index}] ledger account {ledger_id} is inactive."
            )
        allowed_types = set(ledger.get("allowed_document_types") or [])
        if allowed_types and ledger_document_type not in allowed_types:
            raise MoneybirdError(
                f"{field_name}[{index}] ledger account {ledger_id} does not allow {kind}."
            )

        tax_rate = tax_rates.get(tax_id)
        if tax_rate is None:
            raise MoneybirdError(
                f"{field_name}[{index}] tax_rate_id {tax_id or '(empty)'} does not exist."
            )
        if tax_rate.get("active") is False:
            raise MoneybirdError(
                f"{field_name}[{index}] tax rate {tax_id} is inactive."
            )
        tax_type = str(tax_rate.get("tax_rate_type") or "")
        if tax_type and tax_type not in {kind, "purchase_invoice"}:
            raise MoneybirdError(
                f"{field_name}[{index}] tax rate {tax_id} is for {tax_type}, not {kind}."
            )

        validated.append(
            {
                "description": description,
                "price": price,
                "ledger_account_id": ledger_id,
                "tax_rate_id": tax_id,
            }
        )
        total_incl_tax += line_total_incl_tax(
            validated[-1],
            prices_are_incl_tax=bool(prices_are_incl_tax),
            tax_rates=tax_rates,
        )

    return ExplicitDocumentLines(
        lines=tuple(validated),
        total_incl_tax=total_incl_tax.quantize(CENT, rounding=ROUND_HALF_UP),
    )
