"""Regressions for Moneybird quirks that used to be documented but not enforced.

Each case here is a gotcha that CLAUDE.md described in prose while the code still
either crashed on it, or returned something a caller would read as a confident
answer. The point of these tests is that the nuance now lives in the code, so a
caller does not have to remember it.
"""
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird import client as client_module
from moneybird.client import normalize_generic_get_path
from moneybird.config import MoneybirdError
from moneybird.formatting import document_line_quantity, report_period_months
from moneybird.invoicing import document_detail_amount_excl_tax
from moneybird.tools import _context as tool_context
from moneybird.tools import sales as sales_tools


class DocumentLineQuantityTests(unittest.TestCase):
    """Older lines carry '1 x' or '' where a number belongs; Moneybird reads both as 1."""

    def test_blank_and_unit_noise_read_as_moneybird_reads_them(self) -> None:
        for value in (None, "", "   ", "1", 1, "1 x", "1 stuks"):
            with self.subTest(value=value):
                self.assertEqual(document_line_quantity(value), Decimal("1"))

    def test_leading_quantity_survives_a_unit_suffix(self) -> None:
        self.assertEqual(document_line_quantity("2 x"), Decimal("2"))
        self.assertEqual(document_line_quantity("3 stuks"), Decimal("3"))
        self.assertEqual(document_line_quantity("1.5"), Decimal("1.5"))
        self.assertEqual(document_line_quantity(-2), Decimal("-2"))

    def test_ambiguous_quantity_is_refused_rather_than_guessed(self) -> None:
        # '1,5' could be 1.5 or a mistyped 15; this quantity scales a line total,
        # so guessing either way would produce a wrong amount silently.
        for value in ("1,5", "x", "1 x 2", "n/a"):
            with self.subTest(value=value):
                with self.assertRaises(MoneybirdError):
                    document_line_quantity(value)

    def test_reclassify_preview_no_longer_crashes_on_a_1x_line(self) -> None:
        # Regression: this raised decimal.InvalidOperation before the fix.
        detail = {"price": "10.00", "amount": "1 x"}
        self.assertEqual(document_detail_amount_excl_tax(detail), Decimal("10.00"))
        self.assertEqual(
            document_detail_amount_excl_tax({"price": "10.00", "amount": "3 stuks"}),
            Decimal("30.00"),
        )


class ReportPeriodMonthTests(unittest.TestCase):
    """cash_flow/tax/debtors/creditors answer anything over a month with a bare error."""

    def test_month_spans_are_counted_from_explicit_periods(self) -> None:
        self.assertEqual(report_period_months("202606"), ["202606"])
        self.assertEqual(report_period_months("20260401..20260430"), ["202604"])
        self.assertEqual(
            report_period_months("202604..202606"),
            ["202604", "202605", "202606"],
        )
        self.assertEqual(
            report_period_months("20261201..20270131"),
            ["202612", "202701"],
        )

    def test_symbolic_and_blank_periods_resolve_server_side(self) -> None:
        for period in ("", "this_month", "prev_month", "   "):
            with self.subTest(period=period):
                self.assertIsNone(report_period_months(period))

    def test_capped_report_refuses_a_quarter_and_names_the_months(self) -> None:
        with self.assertRaises(MoneybirdError) as caught:
            client_module._reject_over_month_period("tax", "20260401..20260630")
        message = str(caught.exception)
        self.assertIn("at most one month", message)
        for month in ("202604", "202605", "202606"):
            self.assertIn(month, message)

    def test_capped_report_refuses_multi_month_symbols(self) -> None:
        for period in ("this_quarter", "this_year", "prev_quarter"):
            with self.subTest(period=period):
                with self.assertRaises(MoneybirdError) as caught:
                    client_module._reject_over_month_period("cash_flow", period)
                self.assertIn("at most one month", str(caught.exception))

    def test_the_cap_is_a_maximum_not_a_calendar_month(self) -> None:
        # Live-verified: 20260401..20260430 is accepted even though it is a range.
        for period in ("this_month", "202606", "20260401..20260430", ""):
            with self.subTest(period=period):
                client_module._reject_over_month_period("tax", period)

    def test_uncapped_reports_still_take_a_year(self) -> None:
        self.assertEqual(
            report_period_months("20260101..20261231"),
            [f"2026{month:02d}" for month in range(1, 13)],
        )


class AbsentConceptHintTests(unittest.TestCase):
    """Concepts that exist in the product but not the API get a specific message."""

    def test_vat_return_routes_explain_the_settlement_flow_instead(self) -> None:
        for path in ("tax_returns", "vat_returns", "vat_declarations", "vat_documents"):
            with self.subTest(path=path):
                with self.assertRaises(MoneybirdError) as caught:
                    normalize_generic_get_path(path)
                message = str(caught.exception)
                self.assertIn("does not expose VAT returns", message)
                self.assertIn("analyze_vat_settlement", message)

    def test_booking_rule_routes_explain_what_is_observable(self) -> None:
        for path in ("transaction_rules", "boekingsregels", "booking_rules"):
            with self.subTest(path=path):
                with self.assertRaises(MoneybirdError) as caught:
                    normalize_generic_get_path(path)
                message = str(caught.exception)
                self.assertIn("does not expose boekingsregels", message)
                self.assertIn("processed_at", message)

    def test_an_unrelated_bad_route_keeps_the_generic_allowlist_message(self) -> None:
        with self.assertRaises(MoneybirdError) as caught:
            normalize_generic_get_path("nonsense_route")
        self.assertIn("Unsupported generic GET path", str(caught.exception))

    def test_allowlisted_routes_are_unaffected(self) -> None:
        self.assertEqual(normalize_generic_get_path("estimates"), "estimates")


class SupplierSalesInvoiceNoteTests(unittest.TestCase):
    """Zero sales invoices for a supplier is expected, not an absence of invoices."""

    def _client(self, invoices):
        client = mock.Mock()
        client.administration_id = "123"
        client.list_sales_invoices.return_value = invoices
        return client

    def test_empty_contact_filtered_result_points_at_purchase_invoices(self) -> None:
        client = self._client([])
        with mock.patch.object(tool_context, "get_client", return_value=client):
            result = sales_tools.list_sales_invoices(contact_id="42")
        self.assertEqual(result["count"], 0)
        note = result["note"]
        self.assertIn("Contact 42 has no sales invoices.", note)
        self.assertIn("supplier", note)
        self.assertIn("review_purchase_invoices", note)

    def test_a_narrowed_empty_result_does_not_claim_the_contact_has_none(self) -> None:
        # state/reference/period/page can each empty the result on their own, so
        # the note has to name them instead of asserting the contact has no
        # sales invoices at all.
        cases = (
            ({"state": "paid"}, "state=paid"),
            ({"reference": "2026-1"}, "reference=2026-1"),
            ({"period": "202606"}, "period=202606"),
            ({"page": 3}, "page=3"),
        )
        for kwargs, expected in cases:
            with self.subTest(**kwargs):
                client = self._client([])
                with mock.patch.object(tool_context, "get_client", return_value=client):
                    result = sales_tools.list_sales_invoices(contact_id="42", **kwargs)
                note = result["note"]
                self.assertIn(expected, note)
                self.assertIn("does not establish", note)
                self.assertNotIn("Contact 42 has no sales invoices.", note)
                # The supplier explanation is still worth having here.
                self.assertIn("review_purchase_invoices", note)

    def test_several_narrowing_filters_are_all_named(self) -> None:
        client = self._client([])
        with mock.patch.object(tool_context, "get_client", return_value=client):
            result = sales_tools.list_sales_invoices(
                contact_id="42",
                state="draft",
                period="202606",
                page=2,
            )
        note = result["note"]
        for expected in ("state=draft", "period=202606", "page=2"):
            self.assertIn(expected, note)

    def test_state_all_on_the_first_page_is_not_treated_as_narrowing(self) -> None:
        client = self._client([])
        with mock.patch.object(tool_context, "get_client", return_value=client):
            result = sales_tools.list_sales_invoices(
                contact_id="42",
                state="all",
                page=1,
            )
        self.assertIn("Contact 42 has no sales invoices.", result["note"])

    def test_no_note_when_the_contact_does_have_sales_invoices(self) -> None:
        client = self._client([{"id": 1, "state": "paid"}])
        with mock.patch.object(tool_context, "get_client", return_value=client):
            result = sales_tools.list_sales_invoices(contact_id="42")
        self.assertNotIn("note", result)

    def test_no_note_for_an_unfiltered_empty_listing(self) -> None:
        client = self._client([])
        with mock.patch.object(tool_context, "get_client", return_value=client):
            result = sales_tools.list_sales_invoices()
        self.assertNotIn("note", result)


if __name__ == "__main__":
    unittest.main()
