"""Two smaller defects found while auditing a real administration.

*Non-bookable candidates.* Moneybird's email inbox creates a purchase-invoice
shell for every incoming message, so a Terms-of-Service notice arrives as a
document with no lines and a 0.00 total. Matching on counterparty name alone, it
then competes with real invoices for a payment. The discriminator is the absence
of detail lines, never a zero total -- a legitimate zero-value document has lines
and must keep its candidacy.

*Effective list scope.* Moneybird scopes several list endpoints to the current
financial year when the caller passes nothing, and its filters *replace* those
defaults instead of extending them. An unscoped listing that silently covers one
year reads as "these records do not exist", which is the wrong thing to hand an
audit, so the scope is reported rather than left to be inferred.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_scope_"),
)

from moneybird_mcp.bank_matching import (
    NON_BOOKABLE_NO_DETAIL_LINES,
    detail_line_count,
    match_mutation,
    non_bookable_reason,
    score_candidate,
)
from moneybird_mcp.formatting import (
    PERIOD_SOURCE_CALLER,
    PERIOD_SOURCE_MONEYBIRD_DEFAULT,
    PERIOD_SOURCE_UNBOUNDED,
    describe_effective_period,
)

GOOGLE_PAYMENT = {
    "id": "100000000000000901",
    "date": "2026-09-01",
    "amount": "-8.10",
    "amount_open": "-8.10",
    "contra_account_name": "Google Workspace_volts",
    "contra_account_number": "",
    "message": "Google Workspace_volts Dublin IE 01-09-2026 15:05 Pas: 9075",
}

# The real shape: origin "email", no lines, 0.00 total, a recognisable contact.
TOS_EMAIL_SHELL = {
    "id": "100000000000000902",
    "reference": "email",
    "date": "2026-08-28",
    "state": "new",
    "total_price_incl_tax": "0.0",
    "payments": [],
    "details": [],
    "contact": {"company_name": "Google LLC"},
}

REAL_INVOICE = {
    "id": "100000000000000903",
    "reference": "5476802946",
    "date": "2026-08-31",
    "state": "new",
    "total_price_incl_tax": "8.10",
    "payments": [],
    "details": [{"id": "d1", "price": "6.69"}],
    "contact": {"company_name": "Google Cloud EMEA Limited"},
}

# A genuine zero-value document: it has a line, so it stays a normal candidate.
ZERO_VALUE_WITH_LINES = {
    "id": "100000000000000904",
    "reference": "FREE-1",
    "date": "2026-08-28",
    "state": "new",
    "total_price_incl_tax": "0.0",
    "payments": [],
    "details": [{"id": "d1", "price": "0.0", "description": "no charge"}],
    "contact": {"company_name": "Google LLC"},
}


class DetailLineCountTests(unittest.TestCase):
    def test_details_list_is_counted(self):
        self.assertEqual(detail_line_count({"details": [{}, {}]}), 2)
        self.assertEqual(detail_line_count({"details": []}), 0)

    def test_details_count_field_is_used_when_the_list_is_absent(self):
        self.assertEqual(detail_line_count({"details_count": 3}), 3)
        self.assertEqual(detail_line_count({"details_count": "0"}), 0)

    def test_an_unstated_line_count_is_unknown_not_zero(self):
        # Suppressing a document on missing data would hide real invoices.
        self.assertIsNone(detail_line_count({}))
        self.assertIsNone(non_bookable_reason({}))
        self.assertIsNone(non_bookable_reason({"total_price_incl_tax": "0.0"}))


class NonBookableReasonTests(unittest.TestCase):
    def test_email_shell_is_non_bookable(self):
        self.assertEqual(
            non_bookable_reason(TOS_EMAIL_SHELL), NON_BOOKABLE_NO_DETAIL_LINES
        )

    def test_a_zero_total_document_with_lines_stays_bookable(self):
        self.assertIsNone(non_bookable_reason(ZERO_VALUE_WITH_LINES))

    def test_a_normal_invoice_stays_bookable(self):
        self.assertIsNone(non_bookable_reason(REAL_INVOICE))


class ScoreCandidateTests(unittest.TestCase):
    def _score(self, record):
        return score_candidate(
            GOOGLE_PAYMENT, record, booking_type="Document", kind="purchase_invoice"
        )

    def test_the_shell_still_scores_but_is_flagged_non_bookable(self):
        # It is scored, not dropped: the information is useful, the suggestion is not.
        scored = self._score(TOS_EMAIL_SHELL)
        self.assertIsNotNone(scored)
        self.assertFalse(scored["bookable"])
        self.assertEqual(scored["non_bookable_reason"], NON_BOOKABLE_NO_DETAIL_LINES)

    def test_a_zero_value_document_with_lines_is_bookable(self):
        scored = self._score(ZERO_VALUE_WITH_LINES)
        self.assertTrue(scored["bookable"])
        self.assertIsNone(scored["non_bookable_reason"])

    def test_a_matching_invoice_keeps_its_exact_confidence(self):
        scored = self._score(REAL_INVOICE)
        self.assertTrue(scored["bookable"])
        self.assertTrue(scored["amount_matches_exactly"])
        self.assertEqual(scored["confidence"], "strong")


class MatchMutationPartitionTests(unittest.TestCase):
    def _match(self, documents):
        return match_mutation(
            GOOGLE_PAYMENT,
            sales_invoices=[],
            purchase_documents=[("purchase_invoice", item) for item in documents],
        )

    def test_only_shells_leaves_no_suggestion_but_names_them(self):
        result = self._match([TOS_EMAIL_SHELL, dict(TOS_EMAIL_SHELL, id="2")])
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["suggestion"], "none")
        self.assertEqual(len(result["non_bookable_candidates"]), 2)
        self.assertIn("carry no detail lines", result["note"])

    def test_a_real_invoice_wins_and_the_shell_does_not_compete(self):
        result = self._match([TOS_EMAIL_SHELL, REAL_INVOICE])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["candidates"][0]["booking_id"], REAL_INVOICE["id"])
        self.assertEqual(result["suggestion"], "strong")
        self.assertEqual(len(result["non_bookable_candidates"]), 1)

    def test_two_shells_no_longer_produce_a_phantom_ambiguity(self):
        # This is the live failure: two Google "email" notices reported as
        # 'ambiguous' against a real 8.10 payment.
        result = self._match([TOS_EMAIL_SHELL, dict(TOS_EMAIL_SHELL, id="other")])
        self.assertNotEqual(result["suggestion"], "ambiguous")

    def test_a_shell_becomes_a_normal_candidate_once_it_has_lines(self):
        before = self._match([TOS_EMAIL_SHELL])
        self.assertEqual(before["candidates"], [])
        completed = dict(
            TOS_EMAIL_SHELL,
            total_price_incl_tax="8.10",
            details=[{"id": "d1", "price": "6.69"}],
        )
        after = self._match([completed])
        self.assertEqual(len(after["candidates"]), 1)
        self.assertTrue(after["candidates"][0]["amount_matches_exactly"])
        self.assertEqual(after["non_bookable_candidates"], [])

    def test_a_zero_value_document_with_lines_is_still_offered(self):
        result = self._match([ZERO_VALUE_WITH_LINES])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(result["non_bookable_candidates"], [])


class DescribeEffectivePeriodTests(unittest.TestCase):
    def test_an_explicit_period_is_attributed_to_the_caller(self):
        described = describe_effective_period(
            period="20250101..20251231", moneybird_default_period="this_year"
        )
        self.assertEqual(described["period_source"], PERIOD_SOURCE_CALLER)
        self.assertEqual(described["effective_period"], "20250101..20251231")

    def test_a_period_inside_the_raw_filter_also_counts_as_the_caller(self):
        described = describe_effective_period(
            filter="state:late,period:20250101..20251231",
            moneybird_default_period="this_year",
        )
        self.assertEqual(described["period_source"], PERIOD_SOURCE_CALLER)
        self.assertEqual(described["effective_period"], "20250101..20251231")

    def test_no_filter_at_all_reports_moneybirds_documented_default(self):
        described = describe_effective_period(moneybird_default_period="this_year")
        self.assertEqual(described["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)
        self.assertEqual(described["effective_period"], "this_year")
        self.assertNotIn("period_note", described)

    def test_an_undocumented_default_is_admitted_not_invented(self):
        described = describe_effective_period(moneybird_default_period=None)
        self.assertEqual(described["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)
        self.assertEqual(described["effective_period"], "")
        self.assertIn("not documented", described["period_note"])

    def test_another_filter_key_without_a_period_is_unbounded(self):
        # Moneybird replaces its defaults entirely, so state:late alone widens the
        # period rather than keeping the financial year.
        described = describe_effective_period(
            filter="state:late", moneybird_default_period="this_year"
        )
        self.assertEqual(described["period_source"], PERIOD_SOURCE_UNBOUNDED)
        self.assertEqual(described["effective_period"], "")
        self.assertIn("replace its defaults", described["period_note"])

    def test_a_bare_month_is_normalised_like_the_filter_builder_does(self):
        described = describe_effective_period(period="202601")
        self.assertEqual(described["effective_period"], "20260101..20260131")


class ListToolScopeTests(unittest.TestCase):
    """Every list surface that Moneybird can scope implicitly reports its scope."""

    def setUp(self):
        from moneybird_mcp.credentials import set_active_administration_id

        self._env = mock.patch.dict(
            os.environ,
            {"MONEYBIRD_API_TOKEN": "t", "MONEYBIRD_ADMINISTRATION_ID": "scope-admin"},
        )
        self._env.start()
        set_active_administration_id("scope-admin")
        self.addCleanup(self._env.stop)

    class Client:
        administration_id = "scope-admin"

        def list_documents(self, _kind, **_kwargs):
            return []

        def list_sales_invoices(self, **_kwargs):
            return []

        def list_estimates(self, **_kwargs):
            return []

        def list_time_entries(self, **_kwargs):
            return []

        def list_financial_mutations(self, **_kwargs):
            return []

        def list_financial_accounts(self, **_kwargs):
            return []

        def list_ledger_accounts(self):
            return []

    def _call(self, module_name, func_name, **kwargs):
        import importlib

        from moneybird_mcp.tools import _context

        module = importlib.import_module(f"moneybird_mcp.tools.{module_name}")
        with mock.patch.object(_context, "get_client", return_value=self.Client()):
            return getattr(module, func_name)(**kwargs)

    def test_purchase_documents_report_the_documented_year_default(self):
        result = self._call("purchases", "list_purchase_documents")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)
        self.assertEqual(result["effective_period"], "this_year")

    def test_purchase_documents_report_an_unbounded_state_filter(self):
        result = self._call(
            "purchases", "list_purchase_documents", filter="state:late|new"
        )
        self.assertEqual(result["period_source"], PERIOD_SOURCE_UNBOUNDED)

    def test_purchase_documents_report_the_callers_period(self):
        result = self._call(
            "purchases", "list_purchase_documents", period="20250101..20251231"
        )
        self.assertEqual(result["period_source"], PERIOD_SOURCE_CALLER)
        self.assertEqual(result["effective_period"], "20250101..20251231")

    def test_financial_mutations_report_the_documented_year_default(self):
        result = self._call("bank", "list_financial_mutations")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)
        self.assertEqual(result["effective_period"], "this_year")

    def test_financial_mutations_state_filter_alone_is_unbounded(self):
        # This is how the live audit reached 2026 rows from an unscoped call.
        result = self._call(
            "bank", "list_financial_mutations", filter="state:unprocessed"
        )
        self.assertEqual(result["period_source"], PERIOD_SOURCE_UNBOUNDED)

    def test_sales_invoices_do_not_claim_an_undocumented_default(self):
        result = self._call("sales", "list_sales_invoices")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)
        self.assertEqual(result["effective_period"], "")
        self.assertIn("not documented", result["period_note"])

    def test_sales_invoices_state_narrowing_is_reported_as_unbounded(self):
        result = self._call("sales", "list_sales_invoices", state="paid")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_UNBOUNDED)

    def test_sales_invoices_report_the_callers_period(self):
        result = self._call("sales", "list_sales_invoices", period="202601")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_CALLER)
        self.assertEqual(result["effective_period"], "20260101..20260131")

    def test_estimates_report_their_scope(self):
        result = self._call("sales", "list_estimates")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)

    def test_time_entries_report_their_scope(self):
        result = self._call("reference", "list_time_entries")
        self.assertEqual(result["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)

    def test_time_entries_report_the_callers_period(self):
        result = self._call(
            "reference", "list_time_entries", period="20250101..20250331"
        )
        self.assertEqual(result["period_source"], PERIOD_SOURCE_CALLER)

    def test_an_empty_match_scan_still_states_its_scope(self):
        """"Nothing found" is the answer that most needs its scope stated.

        ``suggest_bank_mutation_matches`` returns early when the feed comes back
        empty, and that is exactly the reply an agent turns into "there is nothing
        left to process". With no period passed Moneybird bounds the read to the
        current financial year, so the early return has to carry the same scope
        report the populated one does -- otherwise the one case the feature exists
        for is the one case it does not cover.
        """
        result = self._call("bank", "suggest_bank_mutation_matches")
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["period_source"], PERIOD_SOURCE_MONEYBIRD_DEFAULT)
        self.assertEqual(result["effective_period"], "this_year")

    def test_an_empty_match_scan_attributes_an_explicit_period(self):
        result = self._call(
            "bank", "suggest_bank_mutation_matches", period="20250101..20250331"
        )
        self.assertEqual(result["count"], 0)
        self.assertEqual(result["period_source"], PERIOD_SOURCE_CALLER)
        self.assertEqual(result["effective_period"], "20250101..20250331")
