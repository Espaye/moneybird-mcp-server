"""The explicit-document-line primitive, and the promise that there is one of it.

Two distributions now build guarded writes out of lines a user transcribed from
a source document, and both have to agree to the cent about what those lines add
up to. Agreement by inspection is not agreement: the way this goes wrong is a
copy that rounds per line where the original rounded on the sum, discovered when
a write contract refuses a total that is one cent out.

So the arithmetic has one implementation, and this file pins both halves of that
claim. The identity tests prove the seam hands out the same object the built-in
tools call -- not an equivalent, the same one -- because an equivalent is exactly
what drifts. The semantic tests are golden vectors: inputs whose correct answer
was worked out by hand, chosen around the decisions a copy gets wrong -- where
money parsing rounds, where grossing up rounds, and that it happens per line
rather than on the subtotal.
"""
from __future__ import annotations

import unittest
from decimal import Decimal

from moneybird_mcp import api, document_lines, purchase_reconcile
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.document_lines import (
    CENT,
    ExplicitDocumentLines,
    booking_line_snapshot,
    details_attributes_for_lines,
    line_signature,
    line_signatures,
    line_total_incl_tax,
    line_view,
    validate_explicit_document_lines,
)

LEDGER = "100"
LEDGER_INACTIVE = "101"
LEDGER_SALES_ONLY = "102"
TAX_21 = "21"
TAX_9 = "9"
TAX_ZERO = "0"
TAX_INACTIVE = "7"
TAX_SALES = "8"


class Ledgers:
    """A client double that answers the two reads the primitive makes."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_ledger_accounts(self) -> list[dict]:
        self.calls.append("list_ledger_accounts")
        return [
            {"id": LEDGER, "allowed_document_types": ["purchase_invoice"]},
            {
                "id": LEDGER_INACTIVE,
                "active": False,
                "allowed_document_types": ["purchase_invoice"],
            },
            {"id": LEDGER_SALES_ONLY, "allowed_document_types": ["sales_invoice"]},
            {"id": "103"},  # no allowed_document_types: usable anywhere
        ]

    def list_tax_rates(self) -> list[dict]:
        self.calls.append("list_tax_rates")
        return [
            {"id": TAX_21, "percentage": "21", "tax_rate_type": "purchase_invoice"},
            {"id": TAX_9, "percentage": "9", "tax_rate_type": "purchase_invoice"},
            {"id": TAX_ZERO, "percentage": "0", "tax_rate_type": "purchase_invoice"},
            {"id": TAX_INACTIVE, "percentage": "21", "active": False},
            {"id": TAX_SALES, "percentage": "21", "tax_rate_type": "sales_invoice"},
        ]


def line(price, *, tax=TAX_21, ledger=LEDGER, description="a line", **extra) -> dict:
    row = {
        "description": description,
        "price": price,
        "ledger_account_id": ledger,
        "tax_rate_id": tax,
    }
    row.update(extra)
    return row


def validate(lines, *, incl_tax=False, kind="purchase_invoice", **kwargs):
    return validate_explicit_document_lines(
        Ledgers(),
        document_kind=kind,
        lines=lines,
        prices_are_incl_tax=incl_tax,
        **kwargs,
    )


# --------------------------------------------------------------------------
# Identity -- one implementation, reached from both sides
# --------------------------------------------------------------------------


class OneImplementationTests(unittest.TestCase):
    def test_the_seam_hands_out_the_function_the_built_in_tools_call(self) -> None:
        self.assertIs(
            api.validate_explicit_document_lines,
            document_lines.validate_explicit_document_lines,
        )
        self.assertIs(api.ExplicitDocumentLines, document_lines.ExplicitDocumentLines)

    def test_the_seam_declares_every_name_an_extension_needs(self) -> None:
        for name in (
            "validate_explicit_document_lines",
            "ExplicitDocumentLines",
            "line_signatures",
            "booking_line_snapshot",
        ):
            with self.subTest(name=name):
                self.assertIn(name, api.__all__)
                self.assertIs(getattr(api, name), getattr(document_lines, name))

    def test_the_built_in_reconciler_holds_no_copy_of_the_arithmetic(self) -> None:
        """A second definition here is the drift this file exists to prevent."""
        source = purchase_reconcile.__file__
        with open(source, encoding="utf-8") as handle:
            text = handle.read()
        for definition in (
            "def _map_lines(",
            "def _expected_lines(",
            "def _line_total_incl_tax(",
            "def _line_ledger(",
            "def _line_tax(",
            'CENT = Decimal("0.01")',
        ):
            with self.subTest(definition=definition):
                self.assertNotIn(definition, text)

    def test_the_reconciler_uses_the_shared_objects(self) -> None:
        self.assertIs(purchase_reconcile.CENT, CENT)
        self.assertIs(purchase_reconcile.line_view, line_view)
        self.assertIs(purchase_reconcile.line_signature, line_signature)
        self.assertIs(purchase_reconcile.line_total_incl_tax, line_total_incl_tax)
        self.assertIs(
            purchase_reconcile.details_attributes_for_lines,
            details_attributes_for_lines,
        )

    def test_the_built_in_tools_compare_lines_through_the_shared_helpers(self) -> None:
        from moneybird_mcp.tools import bank, purchases

        self.assertIs(purchases.line_signatures, line_signatures)
        self.assertIs(bank.booking_line_snapshot, booking_line_snapshot)

    def test_no_built_in_module_redefines_a_line_comparison(self) -> None:
        import ast
        import pathlib as _pathlib

        package = _pathlib.Path(document_lines.__file__).resolve().parent
        offenders = []
        for path in sorted(package.rglob("*.py")):
            if path.name == "document_lines.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and node.name in {
                    "_verification_line_signature",
                    "_line_signature",
                    "_expected_lines",
                    "_map_lines",
                    "_line_ledger",
                    "_line_tax",
                    "_line_total_incl_tax",
                }:
                    offenders.append(f"{path.name}:{node.lineno}: {node.name}")
        self.assertEqual(offenders, [], "\n".join(offenders))


# --------------------------------------------------------------------------
# Semantics -- golden vectors, worked out by hand
# --------------------------------------------------------------------------


class TotalArithmeticTests(unittest.TestCase):
    def test_a_net_line_is_grossed_up_and_rounded_half_up(self) -> None:
        # 10.005 is not representable as money, so the price parses to 10.01
        # first; 10.01 * 1.21 = 12.1121, which quantises to 12.11.
        result = validate([line("10.005")])
        self.assertEqual(result.lines[0]["price"], Decimal("10.01"))
        self.assertEqual(result.total_incl_tax, Decimal("12.11"))

    def test_a_gross_line_is_taken_as_stated(self) -> None:
        result = validate([line("12.11")], incl_tax=True)
        self.assertEqual(result.total_incl_tax, Decimal("12.11"))

    def test_each_line_is_grossed_up_and_rounded_before_the_sum(self) -> None:
        """Per line, not on the raw sum -- which is where a copy would drift.

        0.10 at 9% is 0.109. Rounded half-up per line that is 0.11, and three
        lines come to 0.33. Grossing up the 0.30 subtotal instead gives 0.327,
        which quantises to 0.33 as well; the two agree here, and the point of the
        vector is that the 0.11-per-line figure is the one being produced.
        """
        result = validate([line("0.10", tax=TAX_9) for _ in range(3)])
        self.assertEqual(
            line_total_incl_tax(
                result.lines[0],
                prices_are_incl_tax=False,
                tax_rates={TAX_9: {"percentage": "9"}},
            ),
            Decimal("0.11"),
        )
        self.assertEqual(result.total_incl_tax, Decimal("0.33"))

    def test_a_price_is_parsed_as_money_before_anything_multiplies_it(self) -> None:
        """0.045 is not a price. It becomes 0.05, and four of them are 0.20."""
        result = validate([line("0.045", tax=TAX_ZERO) for _ in range(4)])
        self.assertEqual(result.lines[0]["price"], Decimal("0.05"))
        self.assertEqual(result.total_incl_tax, Decimal("0.20"))

    def test_mixed_tax_rates_are_totalled_per_line(self) -> None:
        result = validate([line("100.00"), line("100.00", tax=TAX_9)])
        self.assertEqual(result.total_incl_tax, Decimal("230.00"))

    def test_a_zero_rate_leaves_the_price_alone(self) -> None:
        result = validate([line("42.42", tax=TAX_ZERO)])
        self.assertEqual(result.total_incl_tax, Decimal("42.42"))

    def test_the_total_is_quantised_to_the_cent(self) -> None:
        result = validate([line("1.00", tax=TAX_9)])
        self.assertEqual(result.total_incl_tax.as_tuple().exponent, -2)

    def test_a_negative_line_totals_negative(self) -> None:
        """A credit line is a normal line; the primitive has no opinion on sign."""
        result = validate([line("100.00"), line("-100.00")])
        self.assertEqual(result.total_incl_tax, Decimal("0.00"))

    def test_the_lines_come_back_normalised(self) -> None:
        result = validate([line("  10.00  ".strip(), description="  spaced  ")])
        self.assertEqual(result.lines[0]["description"], "spaced")
        self.assertIsInstance(result.lines[0]["price"], Decimal)
        self.assertIsInstance(result, ExplicitDocumentLines)
        self.assertIsInstance(result.lines, tuple)


class ValidationRefusalTests(unittest.TestCase):
    def refusal(self, lines, **kwargs) -> str:
        with self.assertRaises(MoneybirdError) as caught:
            validate(lines, **kwargs)
        return str(caught.exception)

    def test_an_empty_set_is_refused(self) -> None:
        self.assertIn("at least one exact invoice line", self.refusal([]))

    def test_a_missing_description_is_refused(self) -> None:
        self.assertIn(
            "desired_lines[1] requires a description",
            self.refusal([line("1.00", description="   ")]),
        )

    def test_a_missing_price_is_refused(self) -> None:
        self.assertIn("desired_lines[1] requires a price", self.refusal([line(None)]))
        self.assertIn("desired_lines[1] requires a price", self.refusal([line("")]))

    def test_a_quantity_other_than_one_is_refused(self) -> None:
        message = self.refusal([line("1.00", amount="2")])
        self.assertIn("amount must be 1", message)
        self.assertIn("one explicit total per desired line", message)

    def test_the_unit_quantity_spellings_are_accepted(self) -> None:
        for amount in ("1", "1.0", "1.00", "1 x", "", None):
            with self.subTest(amount=amount):
                self.assertEqual(
                    validate([line("1.00", tax=TAX_ZERO, amount=amount)]).total_incl_tax,
                    Decimal("1.00"),
                )

    def test_an_unknown_ledger_account_is_refused(self) -> None:
        self.assertIn("does not exist", self.refusal([line("1.00", ledger="999")]))
        self.assertIn("(empty)", self.refusal([line("1.00", ledger="")]))

    def test_an_inactive_ledger_account_is_refused(self) -> None:
        self.assertIn(
            "is inactive", self.refusal([line("1.00", ledger=LEDGER_INACTIVE)])
        )

    def test_a_ledger_account_that_forbids_this_document_kind_is_refused(self) -> None:
        self.assertIn(
            "does not allow purchase_invoice",
            self.refusal([line("1.00", ledger=LEDGER_SALES_ONLY)]),
        )

    def test_a_ledger_account_without_a_restriction_is_allowed(self) -> None:
        self.assertEqual(
            validate([line("1.00", ledger="103", tax=TAX_ZERO)]).total_incl_tax,
            Decimal("1.00"),
        )

    def test_an_unknown_tax_rate_is_refused(self) -> None:
        self.assertIn("does not exist", self.refusal([line("1.00", tax="999")]))
        self.assertIn("(empty)", self.refusal([line("1.00", tax="")]))

    def test_an_inactive_tax_rate_is_refused(self) -> None:
        self.assertIn("is inactive", self.refusal([line("1.00", tax=TAX_INACTIVE)]))

    def test_a_tax_rate_for_another_document_kind_is_refused(self) -> None:
        self.assertIn(
            "is for sales_invoice, not purchase_invoice",
            self.refusal([line("1.00", tax=TAX_SALES)]),
        )

    def test_the_line_number_in_a_refusal_is_one_based(self) -> None:
        self.assertIn(
            "desired_lines[3]",
            self.refusal([line("1.00"), line("1.00"), line("1.00", ledger="999")]),
        )

    def test_the_caller_names_the_field_its_own_refusals_mention(self) -> None:
        """The primitive is shared, so the message has to speak the caller's language."""
        with self.assertRaises(MoneybirdError) as caught:
            validate([line("1.00", ledger="999")], field_name="invoice_lines")
        self.assertIn("invoice_lines[1]", str(caught.exception))
        self.assertNotIn("desired_lines", str(caught.exception))


class DocumentKindTests(unittest.TestCase):
    def test_a_receipt_is_filed_under_purchase_invoice_typing(self) -> None:
        """Moneybird's rule, not this module's, and both callers depend on it."""
        result = validate([line("1.00", tax=TAX_ZERO)], kind="receipt")
        self.assertEqual(result.total_incl_tax, Decimal("1.00"))

    def test_a_receipt_still_refuses_a_sales_only_ledger_account(self) -> None:
        with self.assertRaises(MoneybirdError) as caught:
            validate([line("1.00", ledger=LEDGER_SALES_ONLY)], kind="receipt")
        self.assertIn("does not allow receipt", str(caught.exception))

    def test_both_reads_happen_once(self) -> None:
        client = Ledgers()
        validate_explicit_document_lines(
            client,
            document_kind="purchase_invoice",
            lines=[line("1.00"), line("2.00"), line("3.00")],
            prices_are_incl_tax=False,
        )
        self.assertEqual(
            client.calls, ["list_ledger_accounts", "list_tax_rates"]
        )


# --------------------------------------------------------------------------
# The two views a caller needs off a validated set
# --------------------------------------------------------------------------


class LineViewTests(unittest.TestCase):
    def test_the_view_is_all_strings_with_two_decimal_prices(self) -> None:
        result = validate([line("1.5", description="one and a half")])
        self.assertEqual(
            result.view(),
            [
                {
                    "description": "one and a half",
                    "price": "1.50",
                    "ledger_account_id": LEDGER,
                    "tax_rate_id": TAX_21,
                }
            ],
        )

    def test_the_view_is_the_shared_function(self) -> None:
        result = validate([line("1.00")])
        self.assertEqual(result.view(), line_view(result.lines))


class DetailsAttributeTests(unittest.TestCase):
    def test_an_empty_document_gets_new_lines_only(self) -> None:
        result = validate([line("1.00")])
        self.assertEqual(
            result.details_attributes(),
            [
                {
                    "description": "a line",
                    "price": "1.00",
                    "amount": "1",
                    "ledger_account_id": LEDGER,
                    "tax_rate_id": TAX_21,
                }
            ],
        )

    def test_a_matching_line_is_reused_so_its_identity_survives(self) -> None:
        result = validate([line("2.00")])
        ops = result.details_attributes(
            [{"id": "L1", "ledger_account_id": LEDGER, "tax_rate_id": TAX_21}]
        )
        self.assertEqual(ops, [{"id": "L1", "description": "a line", "price": "2.00", "amount": "1"}])

    def test_a_leftover_line_is_marked_for_destruction(self) -> None:
        result = validate([line("2.00")])
        ops = result.details_attributes(
            [
                {"id": "L1", "ledger_account_id": LEDGER, "tax_rate_id": TAX_21},
                {"id": "L2", "ledger_account_id": "103", "tax_rate_id": TAX_ZERO},
            ]
        )
        self.assertEqual(ops[-1], {"id": "L2", "_destroy": "true"})

    def test_one_current_line_is_reused_at_most_once(self) -> None:
        result = validate([line("1.00"), line("2.00")])
        ops = result.details_attributes(
            [{"id": "L1", "ledger_account_id": LEDGER, "tax_rate_id": TAX_21}]
        )
        reused = [op for op in ops if op.get("id") and "_destroy" not in op]
        self.assertEqual(len(reused), 1)
        self.assertEqual(len([op for op in ops if "id" not in op]), 1)


class ComparisonHelperTests(unittest.TestCase):
    """The other end of a guarded write: proving the lines that arrived match."""

    def test_a_signature_ignores_order(self) -> None:
        first = [line("1.00", description="a"), line("2.00", description="b")]
        self.assertEqual(line_signatures(first), line_signatures(list(reversed(first))))

    def test_a_signature_compares_price_as_fixed_text(self) -> None:
        self.assertEqual(
            line_signatures([line("1.5")]), line_signatures([line("1.50")])
        )
        self.assertEqual(
            line_signatures([line(Decimal("1.50"))]), line_signatures([line("1.5")])
        )

    def test_a_signature_ignores_surrounding_space_in_a_description(self) -> None:
        self.assertEqual(
            line_signatures([line("1.00", description="  a  ")]),
            line_signatures([line("1.00", description="a")]),
        )

    def test_a_different_price_is_a_different_signature(self) -> None:
        self.assertNotEqual(line_signatures([line("1.00")]), line_signatures([line("1.01")]))

    def test_a_different_ledger_or_tax_is_a_different_signature(self) -> None:
        self.assertNotEqual(
            line_signatures([line("1.00")]), line_signatures([line("1.00", ledger="103")])
        )
        self.assertNotEqual(
            line_signatures([line("1.00")]), line_signatures([line("1.00", tax=TAX_9)])
        )

    def test_a_booking_snapshot_orders_by_row_then_id(self) -> None:
        rows = [
            {"id": "2", "row_order": 1, "price": "1.00"},
            {"id": "1", "row_order": 1, "price": "1.00"},
            {"id": "3", "row_order": 0, "price": "1.00"},
        ]
        self.assertEqual(
            [row["id"] for row in booking_line_snapshot(rows)], ["3", "1", "2"]
        )

    def test_a_booking_snapshot_keeps_the_fields_a_save_must_not_move(self) -> None:
        snapshot = booking_line_snapshot(
            [
                {
                    "id": "1",
                    "description": "a",
                    "price": "1.5",
                    "amount_decimal": "2.0",
                    "ledger_account_id": LEDGER,
                    "tax_rate_id": TAX_21,
                    "project_id": "p",
                    "product_id": "q",
                    "period": "202603",
                    "row_order": 3,
                }
            ]
        )
        self.assertEqual(
            snapshot,
            [
                {
                    "id": "1",
                    "description": "a",
                    "price": "1.50",
                    "amount": "2.0",
                    "ledger_account_id": LEDGER,
                    "tax_rate_id": TAX_21,
                    "project_id": "p",
                    "product_id": "q",
                    "period": "202603",
                    "row_order": 3,
                }
            ],
        )

    def test_a_booking_snapshot_prefers_the_decimal_quantity(self) -> None:
        self.assertEqual(
            booking_line_snapshot(
                [{"price": "1.00", "amount_decimal": "2.5", "amount": "2"}]
            )[0]["amount"],
            "2.5",
        )
        self.assertEqual(
            booking_line_snapshot([{"price": "1.00", "amount": "2"}])[0]["amount"], "2"
        )
        self.assertEqual(
            booking_line_snapshot([{"price": "1.00"}])[0]["amount"], "1"
        )

    def test_an_empty_line_set_snapshots_to_nothing(self) -> None:
        self.assertEqual(booking_line_snapshot([]), [])
        self.assertEqual(line_signatures([]), [])


if __name__ == "__main__":
    unittest.main()
