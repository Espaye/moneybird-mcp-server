"""End-to-end VAT settlement-evidence scenarios through the MCP tools.

The original defect was not in the arithmetic but in *which documents the tool
looked at*, and the write preflight shared the blind spot, so these drive
``analyze_vat_settlement`` and ``prepare_vat_settlement_journal`` rather than the
pure helpers. Every ledger shape here is modelled on a real administration where
Moneybird had settled a quarter with its own ``VatDocument``.
"""
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_vat_evidence_"),
)

from test_vat_settlement import (
    PAYABLE,
    RECEIVABLE,
    SETTLEMENT,
    FakeClient,
)
from test_vat_settlement_evidence import Q2, _row

from moneybird_mcp.config import MoneybirdError


def _rich_ledger(per_account):
    """A general_ledger report able to state both sides of every account."""

    return {
        "debit_sums": {
            "ledger_accounts": [
                {"ledger_account_id": account, "value": debit}
                for account, (debit, _credit) in per_account.items()
            ]
        },
        "credit_sums": {
            "ledger_accounts": [
                {"ledger_account_id": account, "value": credit}
                for account, (_debit, credit) in per_account.items()
            ]
        },
    }


def _accrual(account_id, amount, date="2026-04-30", document_id=None):
    return _row(
        account_id,
        amount,
        document_id=document_id or f"accrual-{account_id}",
        document_type="Document",
        date=date,
    )


# Q2 cleared by a VatDocument: each VAT account carries its accrual *and* the
# clearing, so both net to zero -- the live signature of a settled quarter, and
# the reason a per-account sign proof alone would be undecidable here.
SETTLED_LEDGER = _rich_ledger(
    {
        PAYABLE: ("5232.05", "5232.05"),
        RECEIVABLE: ("808.00", "808.00"),
        SETTLEMENT: ("4424.05", "0.00"),
    }
)

SETTLED_ROWS = [
    _accrual(PAYABLE, "5232.05"),
    _accrual(RECEIVABLE, "-808.00"),
    _row(PAYABLE, "-5232.05", document_id="vat-doc-q2", date="2026-06-30"),
    _row(RECEIVABLE, "808.00", document_id="vat-doc-q2", date="2026-06-30"),
    _row(SETTLEMENT, "-4424.05", document_id="vat-doc-q2", date="2026-06-30"),
]


class _ToolTestBase(unittest.TestCase):
    def setUp(self):
        from moneybird_mcp.credentials import set_active_administration_id

        self._env = mock.patch.dict(
            os.environ,
            {"MONEYBIRD_API_TOKEN": "t", "MONEYBIRD_ADMINISTRATION_ID": "vat-admin"},
        )
        self._env.start()
        set_active_administration_id("vat-admin")
        self.addCleanup(self._env.stop)

    def _analyze(self, client, period=Q2):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        with mock.patch.object(_context, "get_client", return_value=client):
            return ledger_tools.analyze_vat_settlement(period=period)

    def _prepare(self, client, period=Q2, **kwargs):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        params = {
            "reference": "BTW-2026-Q2",
            "period": period,
            "declared_amount": "4423.00",
            "date": "",
        }
        params.update(kwargs)
        with mock.patch.object(_context, "get_client", return_value=client):
            return ledger_tools.prepare_vat_settlement_journal(**params)


class VatDocumentSettledPeriodTests(_ToolTestBase):
    def _client(self, rows=None, ledger=None):
        return FakeClient(
            ledger_override=SETTLED_LEDGER if ledger is None else ledger,
            journal_entry_rows=SETTLED_ROWS if rows is None else rows,
        )

    def test_analysis_reports_the_vat_accounts_as_cleared(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertTrue(status["vat_accounts_cleared_in_period"])
        self.assertEqual(len(status["clearing_occurrences"]), 1)
        occurrence = status["clearing_occurrences"][0]
        self.assertEqual(occurrence["document_type"], "VatDocument")
        self.assertEqual(occurrence["document_id"], "vat-doc-q2")
        self.assertFalse(status["safe_to_settle"])
        self.assertTrue(status["settlement_evidence_complete"])

    def test_filing_and_payment_are_never_inferred_from_the_ledger(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertEqual(
            status["filed_with_tax_authority"], "not_exposed_by_moneybird_api"
        )
        self.assertEqual(
            status["tax_paid_or_refunded"],
            "derive_from_settlement_account_bank_mutations",
        )

    def test_deprecated_alias_agrees_with_the_new_field(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertEqual(
            status["already_settled"], status["vat_accounts_cleared_in_period"]
        )

    def test_amounts_are_reconstructed_via_the_account_type_proof(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertTrue(status["amounts_reconstructed"])
        self.assertIsNone(status["amounts_withheld_reason"])
        occurrence = status["clearing_occurrences"][0]
        self.assertEqual(occurrence["payable_restore"], "5232.05")
        self.assertEqual(occurrence["receivable_restore"], "808.00")
        self.assertEqual(occurrence["settlement_amount"], "4424.05")
        # Both VAT accounts are symmetrical; the unambiguous settlement account
        # carries the direction. That inference has to be visible, not implied.
        self.assertIn(
            "carried from", status["journal_entry_sign_mappings"]["payable"]["reason"]
        )
        self.assertTrue(status["journal_entry_sign_mappings"]["settlement"]["proven"])

    def test_the_reconstructed_gross_still_explains_the_reverse_charge(self):
        result = self._analyze(self._client())
        self.assertEqual(result["gross_movements"]["payable_net_credit"], "5232.05")
        self.assertEqual(result["gross_movements"]["receivable_net_debit"], "808.00")
        self.assertEqual(result["gross_vs_reported"]["offsetting_amount"], "6.30")
        self.assertFalse(result["gross_vs_reported"]["is_anomaly"])
        self.assertEqual(
            result["gross_movements"]["basis"],
            "reconstructed_before_existing_settlement_journals",
        )

    def test_the_write_is_refused_for_an_already_cleared_period(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(self._client())
        message = str(caught.exception)
        self.assertIn("already been cleared", message)
        self.assertIn("VatDocument", message)

    def test_a_settlement_account_only_document_neither_clears_nor_blocks(self):
        # A bank payment allocated to a VAT return uses booking_type VatDocument
        # and touches the settlement account only. It clears nothing.
        rows = [
            _accrual(PAYABLE, "5232.05"),
            _accrual(RECEIVABLE, "-808.00"),
            _row(SETTLEMENT, "-4424.05", document_id="vat-payment", date="2026-06-30"),
        ]
        ledger = _rich_ledger(
            {
                PAYABLE: ("0.00", "5232.05"),
                RECEIVABLE: ("808.00", "0.00"),
                SETTLEMENT: ("4424.05", "0.00"),
            }
        )
        status = self._analyze(self._client(rows=rows, ledger=ledger))[
            "settlement_status"
        ]
        self.assertFalse(status["vat_accounts_cleared_in_period"])
        self.assertEqual(status["clearing_occurrences"], [])
        self.assertTrue(status["safe_to_settle"])

    def test_two_vat_documents_are_both_reported_and_both_block(self):
        rows = SETTLED_ROWS + [
            _row(
                RECEIVABLE,
                "1.00",
                document_id="vat-doc-q2-suppletie",
                date="2026-06-30",
            ),
            _row(
                SETTLEMENT,
                "-1.00",
                document_id="vat-doc-q2-suppletie",
                date="2026-06-30",
            ),
        ]
        ledger = _rich_ledger(
            {
                PAYABLE: ("5232.05", "5232.05"),
                RECEIVABLE: ("808.00", "809.00"),
                SETTLEMENT: ("4425.05", "0.00"),
            }
        )
        status = self._analyze(self._client(rows=rows, ledger=ledger))[
            "settlement_status"
        ]
        self.assertEqual(len(status["clearing_occurrences"]), 2)
        with self.assertRaises(MoneybirdError):
            self._prepare(self._client(rows=rows, ledger=ledger))


class PartiallyClearedPeriodTests(_ToolTestBase):
    """Only the input side was cleared; the output side is still standing."""

    def _client(self):
        rows = [
            _accrual(PAYABLE, "5232.05"),
            _accrual(RECEIVABLE, "-808.00"),
            _row(RECEIVABLE, "808.00", document_id="vat-doc-partial", date="2026-06-30"),
            _row(
                SETTLEMENT, "-808.00", document_id="vat-doc-partial", date="2026-06-30"
            ),
        ]
        ledger = _rich_ledger(
            {
                PAYABLE: ("0.00", "5232.05"),
                RECEIVABLE: ("808.00", "808.00"),
                SETTLEMENT: ("808.00", "0.00"),
            }
        )
        return FakeClient(ledger_override=ledger, journal_entry_rows=rows)

    def test_partial_clearing_is_detected(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertTrue(status["vat_accounts_cleared_in_period"])
        self.assertEqual(
            status["clearing_occurrences"][0]["touched_roles"],
            ["receivable", "settlement"],
        )

    def test_partial_clearing_blocks_an_automatic_settlement(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(self._client())
        self.assertIn("already been cleared", str(caught.exception))

    def test_the_uncleared_output_side_stays_visible(self):
        result = self._analyze(self._client())
        self.assertEqual(
            result["gross_movements"]["current_period_net_after_journals"][
                "payable_net_credit"
            ],
            "5232.05",
        )


class ResidualAfterTheReturnTests(_ToolTestBase):
    """A purchase invoice entered after the return was filed, as seen live."""

    def _client(self):
        rows = SETTLED_ROWS + [
            _accrual(RECEIVABLE, "-34.97", date="2026-06-17", document_id="late-invoice")
        ]
        ledger = _rich_ledger(
            {
                PAYABLE: ("5232.05", "5232.05"),
                RECEIVABLE: ("842.97", "808.00"),
                SETTLEMENT: ("4424.05", "0.00"),
            }
        )
        return FakeClient(ledger_override=ledger, journal_entry_rows=rows)

    def test_the_late_invoice_is_a_residual_not_an_unsettled_period(self):
        result = self._analyze(self._client())
        self.assertTrue(result["settlement_status"]["vat_accounts_cleared_in_period"])
        # 842.97 debit - 808.00 credit = 34.97 still standing after the clearing.
        self.assertEqual(
            result["gross_movements"]["current_period_net_after_journals"][
                "receivable_net_debit"
            ],
            "34.97",
        )

    def test_a_residual_does_not_reopen_the_period(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(self._client())
        self.assertIn("already been cleared", str(caught.exception))


class SignProofFailureTests(_ToolTestBase):
    """An unprovable direction withholds amounts without weakening the guard."""

    def _client(self):
        # Every account of this type is symmetrical, so no direction can be proven
        # and none can be carried. The clearing still touches the settlement
        # account, which is the sign-free gate, so it is detected regardless.
        rows = [
            _accrual(PAYABLE, "100.00"),
            _row(PAYABLE, "-100.00", document_id="vat-doc-q2", date="2026-06-30"),
            _accrual(RECEIVABLE, "-100.00"),
            _row(RECEIVABLE, "100.00", document_id="vat-doc-q2", date="2026-06-30"),
            _row(SETTLEMENT, "-100.00", document_id="vat-doc-q2", date="2026-06-30"),
            _accrual(SETTLEMENT, "100.00", date="2026-05-31", document_id="refund"),
        ]
        ledger = _rich_ledger(
            {
                PAYABLE: ("100.00", "100.00"),
                RECEIVABLE: ("100.00", "100.00"),
                SETTLEMENT: ("100.00", "100.00"),
            }
        )
        return FakeClient(ledger_override=ledger, journal_entry_rows=rows)

    def test_amounts_are_withheld_with_a_stated_reason(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertTrue(status["vat_accounts_cleared_in_period"])
        self.assertFalse(status["amounts_reconstructed"])
        self.assertIn("withheld", status["amounts_withheld_reason"])
        occurrence = status["clearing_occurrences"][0]
        self.assertIsNone(occurrence["payable_restore"])
        self.assertEqual(
            occurrence["unproven_roles"], ["payable", "receivable", "settlement"]
        )
        self.assertEqual(
            occurrence["matched_rule"], "gross_vat_account_plus_settlement_account"
        )

    def test_the_basis_says_the_amounts_were_withheld(self):
        result = self._analyze(self._client())
        self.assertEqual(
            result["gross_movements"]["basis"],
            "current_period_ledger_movements_amounts_withheld_sign_unproven",
        )

    def test_the_write_is_still_refused(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(self._client())
        self.assertIn("already been cleared", str(caught.exception))


class MissingEvidenceTests(_ToolTestBase):
    """No rows for an account the ledger says moved is a gap, not a clean period."""

    def _client(self):
        return FakeClient(
            ledger_override=_rich_ledger(
                {
                    PAYABLE: ("0.00", "5232.05"),
                    RECEIVABLE: ("808.00", "0.00"),
                    SETTLEMENT: ("0.00", "0.00"),
                }
            ),
            journal_entry_rows=[],
        )

    def test_analysis_refuses_to_call_the_period_clean(self):
        status = self._analyze(self._client())["settlement_status"]
        self.assertFalse(status["vat_accounts_cleared_in_period"])
        self.assertFalse(status["settlement_evidence_complete"])
        self.assertFalse(status["safe_to_settle"])
        self.assertEqual(len(status["settlement_evidence_gaps"]), 2)
        self.assertIn("not a clean bill of health", status["message"])

    def test_the_write_refuses_on_unproven_evidence(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(self._client())
        self.assertIn("could not be established", str(caught.exception))


class ExplainedRoundingAdjustmentToolTests(_ToolTestBase):
    """The Q4-2025 shape: a 3.26 gap the books already explain, end to end."""

    ROUNDING_JOURNAL = {
        "id": "round-fix",
        "reference": "Correctie verschil btw",
        "date": "2026-06-30",
        "general_journal_document_entries": [
            {"ledger_account_id": RECEIVABLE, "debit": "3.26", "credit": "0.00"},
            # ROUNDING is resolved from the chart by name, not referenced by id here.
            {"ledger_account_id": "100000000000000104", "debit": "0.00", "credit": "3.26"},
        ],
    }

    def _client(self, journals=None):
        # Receivable carries 3.26 more than the return reported, because the books
        # were trued up to the declared whole-euro figure.
        return FakeClient(
            ledger_override=_rich_ledger(
                {
                    PAYABLE: ("0.00", "5232.05"),
                    RECEIVABLE: ("811.26", "0.00"),
                    SETTLEMENT: ("0.00", "0.00"),
                }
            ),
            general_journals=self.ROUNDING_JOURNAL if journals is None else journals,
        )

    def test_without_the_journal_the_gap_is_an_anomaly(self):
        result = self._analyze(self._client(journals=[]))
        self.assertEqual(result["gross_vs_reported"]["unexplained_deductible"], "3.26")
        self.assertEqual(result["gross_vs_reported"]["residual_deductible"], "3.26")
        self.assertTrue(result["reconciliation"]["is_anomaly"])

    def test_the_journal_explains_the_gap_and_clears_the_anomaly(self):
        result = self._analyze(self._client(journals=[self.ROUNDING_JOURNAL]))
        reconciliation = result["reconciliation"]
        self.assertEqual(reconciliation["total_discrepancy"]["deductible"], "3.26")
        self.assertEqual(reconciliation["explained_totals"]["deductible"], "3.26")
        self.assertEqual(reconciliation["unexplained_residual"]["deductible"], "0.00")
        self.assertFalse(reconciliation["is_anomaly"])
        self.assertFalse(result["gross_vs_reported"]["is_anomaly"])

    def test_the_explanation_is_itemised_not_netted_away(self):
        reconciliation = self._analyze(
            self._client(journals=[self.ROUNDING_JOURNAL])
        )["reconciliation"]
        self.assertEqual(len(reconciliation["explained_adjustments"]), 1)
        adjustment = reconciliation["explained_adjustments"][0]
        self.assertEqual(adjustment["document_id"], "round-fix")
        self.assertEqual(adjustment["receivable_adjustment"], "3.26")
        self.assertEqual(adjustment["category"], "vat_rounding_correction")
        self.assertEqual(
            reconciliation["rounding_account"]["name"], "Afrondingsverschillen"
        )

    def test_a_partial_explanation_keeps_its_residual_anomalous(self):
        partial = dict(
            self.ROUNDING_JOURNAL,
            general_journal_document_entries=[
                {"ledger_account_id": RECEIVABLE, "debit": "1.00", "credit": "0.00"},
                {
                    "ledger_account_id": "100000000000000104",
                    "debit": "0.00",
                    "credit": "1.00",
                },
            ],
        )
        reconciliation = self._analyze(self._client(journals=[partial]))[
            "reconciliation"
        ]
        self.assertEqual(reconciliation["explained_totals"]["deductible"], "1.00")
        self.assertEqual(reconciliation["unexplained_residual"]["deductible"], "2.26")
        self.assertTrue(reconciliation["is_anomaly"])

    def test_an_administration_without_a_rounding_account_still_analyses(self):
        from test_vat_settlement import NoRoundingExactClient

        result = self._analyze(
            NoRoundingExactClient(general_journals=[self.ROUNDING_JOURNAL]),
            period="20260401..20260430",
        )
        self.assertIsNone(result["reconciliation"]["rounding_account"])
        self.assertEqual(result["reconciliation"]["explained_adjustments"], [])
        self.assertIn("rounding", result["reconciliation"]["unresolved_optional_roles"])


class JournalEntryPaginationTests(_ToolTestBase):
    """A settlement hiding on page two must still be found."""

    def _client(self):
        rows = [
            _accrual(RECEIVABLE, "-1.00", document_id=f"accrual-{index}")
            for index in range(100)
        ]
        rows.append(
            _row(RECEIVABLE, "100.00", document_id="vat-doc-q2", date="2026-04-30")
        )
        rows.append(
            _row(SETTLEMENT, "-100.00", document_id="vat-doc-q2", date="2026-04-30")
        )
        ledger = _rich_ledger(
            {
                PAYABLE: ("0.00", "0.00"),
                RECEIVABLE: ("100.00", "100.00"),
                SETTLEMENT: ("100.00", "0.00"),
            }
        )
        return FakeClient(ledger_override=ledger, journal_entry_rows=rows)

    def test_the_scan_walks_past_the_first_page(self):
        client = self._client()
        status = self._analyze(client)["settlement_status"]
        pages = [call for call in client.journal_entry_calls if call[1] == RECEIVABLE]
        self.assertGreaterEqual(max(page for _month, _account, page in pages), 2)
        self.assertEqual(status["journal_entry_row_counts"]["receivable"], 101)
        self.assertTrue(status["vat_accounts_cleared_in_period"])

    def test_a_settlement_only_on_page_two_still_blocks_the_write(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(self._client())
        self.assertIn("already been cleared", str(caught.exception))
