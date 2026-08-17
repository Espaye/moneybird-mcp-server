"""Regressions for Moneybird quirks that used to be documented but not enforced.

Each case here is a previously identified edge case that the code still
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

from moneybird_mcp import client as client_module
from moneybird_mcp.client import normalize_generic_get_path
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.formatting import (
    build_filter_string,
    document_line_quantity,
    normalize_list_period,
    report_period_months,
)
from moneybird_mcp.invoicing import document_detail_amount_excl_tax
from moneybird_mcp.tools import _context as tool_context
from moneybird_mcp.tools import sales as sales_tools


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


class ListPeriodNormalizationTests(unittest.TestCase):
    """A bare month is valid on reports but rejected by every collection endpoint.

    Moneybird answers 'period:202601' on a list route with HTTP 400 ('Period is
    invalid', or 'Period must have both a start and end date' on sales
    invoices), while the same value is fine on a report. Callers cannot be
    expected to track which half of the API they are on, so the bare month is
    widened to the day range it means.
    """

    def test_bare_month_becomes_that_months_day_range(self) -> None:
        self.assertEqual(normalize_list_period("202601"), "20260101..20260131")
        self.assertEqual(normalize_list_period("202607"), "20260701..20260731")

    def test_month_length_follows_the_calendar(self) -> None:
        self.assertEqual(normalize_list_period("202602"), "20260201..20260228")
        self.assertEqual(normalize_list_period("202402"), "20240201..20240229")

    def test_month_ranges_widen_to_cover_whole_end_months(self) -> None:
        self.assertEqual(normalize_list_period("202604..202606"), "20260401..20260630")
        self.assertEqual(normalize_list_period("202601..20260215"), "20260101..20260215")

    def test_symbolic_and_day_periods_are_left_for_the_server(self) -> None:
        for period in ("", "this_month", "prev_year", "20260101..20260131"):
            with self.subTest(period=period):
                self.assertEqual(normalize_list_period(period), period)

    def test_unparseable_periods_pass_through_rather_than_guessing(self) -> None:
        for period in ("abc", "202613", "2026013", "20260101.."):
            with self.subTest(period=period):
                self.assertEqual(normalize_list_period(period), period)

    def test_a_period_inside_the_raw_filter_is_normalized_too(self) -> None:
        self.assertEqual(
            build_filter_string(filter="state:unprocessed,period:202607"),
            "state:unprocessed,period:20260701..20260731",
        )
        self.assertEqual(
            build_filter_string(filter="state:unprocessed", period="202607"),
            "state:unprocessed,period:20260701..20260731",
        )


class WidePeriodChunkingTests(unittest.TestCase):
    """Moneybird refuses a period holding too many mutations instead of truncating."""

    def setUp(self) -> None:
        self.client = client_module.MoneybirdClient.__new__(
            client_module.MoneybirdClient
        )
        self.too_many = client_module.MoneybirdHTTPError(
            "Moneybird returned HTTP 400 for operation /:id/financial_mutations.json. "
            "Moneybird reported: Too many financial mutations to return, please use sync API",
            status_code=400,
        )

    def test_a_year_splits_into_one_filter_per_month(self) -> None:
        chunks = self.client._period_month_chunks("period:this_year", self.too_many)
        self.assertIsNotNone(chunks)
        self.assertTrue(all(part.startswith("period:") for part in chunks))
        self.assertIn("..", chunks[0])

    def test_other_filter_terms_survive_the_split(self) -> None:
        chunks = self.client._period_month_chunks(
            "state:unprocessed,period:20260101..20260331", self.too_many
        )
        self.assertEqual(
            chunks,
            [
                "state:unprocessed,period:20260101..20260131",
                "state:unprocessed,period:20260201..20260228",
                "state:unprocessed,period:20260301..20260331",
            ],
        )

    def test_a_different_rejection_is_not_silently_retried(self) -> None:
        other = client_module.MoneybirdHTTPError(
            "Moneybird returned HTTP 400. Moneybird reported: Period is invalid",
            status_code=400,
        )
        self.assertIsNone(self.client._period_month_chunks("period:this_year", other))

    def test_nothing_to_split_returns_none(self) -> None:
        self.assertIsNone(
            self.client._period_month_chunks("period:202607", self.too_many)
        )
        self.assertIsNone(
            self.client._period_month_chunks("state:unprocessed", self.too_many)
        )


class ChunkedPeriodBoundaryTests(unittest.TestCase):
    """Splitting a period must not widen it.

    A partial range covers three months but not all of the first or last one.
    Retrying on whole months would return records outside the range the caller
    asked for, which reads as data rather than as an error.
    """

    def setUp(self) -> None:
        self.client = client_module.MoneybirdClient.__new__(
            client_module.MoneybirdClient
        )
        self.too_many = client_module.MoneybirdHTTPError(
            "Moneybird reported: Too many financial mutations to return",
            status_code=400,
        )

    def test_partial_months_keep_the_requested_day_endpoints(self) -> None:
        self.assertEqual(
            self.client._period_month_chunks(
                "period:20260115..20260310", self.too_many
            ),
            [
                "period:20260115..20260131",
                "period:20260201..20260228",
                "period:20260301..20260310",
            ],
        )

    def test_whole_month_ranges_are_unchanged(self) -> None:
        self.assertEqual(
            self.client._period_month_chunks(
                "period:20260101..20260228", self.too_many
            ),
            ["period:20260101..20260131", "period:20260201..20260228"],
        )

    def test_a_later_page_explains_itself_instead_of_repeating_moneybird(
        self,
    ) -> None:
        client = client_module.MoneybirdClient.__new__(client_module.MoneybirdClient)
        client.administration_id = "1"

        def _fail(*args, **kwargs):
            raise self.too_many

        client._request = _fail
        with self.assertRaises(client_module.MoneybirdHTTPError) as caught:
            client.list_financial_mutations(period="this_year", page=2)
        self.assertIn("one month at a time", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 400)
