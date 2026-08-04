"""Regression tests for clearing a filed VAT period.

The anchor case is a quarter in which reverse-charge VAT (btw verlegd) inflates
both gross ledger movements without touching the net position. Clearing only the
amounts visible in the tax report would strand that amount on both accounts, so
the offsetting pair is asserted explicitly here.

All figures in this module are synthetic; only the arithmetic relationships
between them are drawn from a settlement seen in practice.
"""
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_vat_"),
)

from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.vat_settlement import (
    LedgerMovement,
    build_vat_settlement_journal,
    compare_gross_to_reported,
    find_vat_settlement_journals,
    ledger_movements_from_report,
    month_periods,
    reported_vat_totals,
    resolve_vat_accounts,
    settlement_preflight,
)

PAYABLE = "100000000000000101"
RECEIVABLE = "100000000000000102"
SETTLEMENT = "100000000000000103"
ROUNDING = "100000000000000104"

LEDGER_ACCOUNTS = [
    {"id": PAYABLE, "name": "Te betalen btw", "account_id": "16224", "account_type": "current_liabilities"},
    {"id": RECEIVABLE, "name": "Te vorderen btw", "account_id": "16225", "account_type": "current_liabilities"},
    {"id": SETTLEMENT, "name": "Betaalde en/of ontvangen btw", "account_id": "16221", "account_type": "current_liabilities"},
    {"id": ROUNDING, "name": "Afrondingsverschillen", "account_id": "46425", "account_type": "expenses"},
]

# Gross ledger turnover: both sides carry reverse-charge VAT on top of the
# reported rubrieken -- 2.10 per month, 6.30 over the quarter.
def _general_ledger(payable_credit: str, receivable_debit: str):
    return {
        "debit_sums": {"ledger_accounts": [{"ledger_account_id": RECEIVABLE, "value": receivable_debit}]},
        "credit_sums": {"ledger_accounts": [{"ledger_account_id": PAYABLE, "value": payable_credit}]},
    }


Q2_GENERAL_LEDGER = _general_ledger("5232.05", "808.00")
GENERAL_LEDGERS = {
    "20260401..20260630": Q2_GENERAL_LEDGER,
    "20260401..20260430": _general_ledger("1252.10", "216.50"),
}

# The tax reports for the same period, per month, because Moneybird's tax report
# refuses a period longer than one month. The reverse-charge row reports zero tax
# even though it moved both ledger accounts -- which is exactly why the two views
# are compared rather than merged.
def _tax_report(sales, purchase_21, purchase_9):
    return {
        "tax_rates": [
            {"name": "21% btw", "report_reference": "NL/1a", "type": "sales_invoice", "tax": sales},
            {"name": "21% btw", "report_reference": "NL/5b", "type": "purchase_invoice", "tax": purchase_21},
            {"name": "9% btw", "report_reference": "NL/5b", "type": "purchase_invoice", "tax": purchase_9},
            {"name": "Binnen EU 21% (btw verlegd)", "report_reference": "NL/4b, NL/5b", "type": "purchase_invoice", "tax": "0.0"},
        ]
    }


Q2_TAX_REPORTS = {
    "202604": _tax_report("1250.00", "210.35", "4.05"),
    "202605": _tax_report("1875.50", "260.35", "6.35"),
    "202606": _tax_report("2100.25", "315.20", "5.40"),
}

DECLARED_Q2 = Decimal("4423.00")


def _q2_settlement_journal(*, reference="VAT settlement already booked"):
    return {
        "id": "journal-existing",
        "reference": reference,
        "date": "2026-06-30",
        "general_journal_document_entries": [
            {"ledger_account_id": PAYABLE, "debit": "5232.05", "credit": "0.00"},
            {"ledger_account_id": RECEIVABLE, "debit": "0.00", "credit": "808.00"},
            {"ledger_account_id": SETTLEMENT, "debit": "0.00", "credit": "4423.00"},
            {"ledger_account_id": ROUNDING, "debit": "0.00", "credit": "1.05"},
        ],
    }


def accounts_by_role():
    return resolve_vat_accounts(LEDGER_ACCOUNTS)


class FakeClient:
    """Minimal stand-in exposing only what the settlement tools touch."""

    # Must match the active administration the approval store binds to, or
    # pop_approval rejects the token before the executor ever runs.
    administration_id = "vat-admin"

    def __init__(
        self,
        *,
        general_journals=None,
        period_locked_until=None,
        ledger_override=None,
    ):
        self.general_journals = general_journals or []
        self.period_locked_until = period_locked_until
        self.ledger_override = ledger_override
        self.created = []

    def require_current_administration_access(self):
        return {"id": self.administration_id, "period_locked_until": self.period_locked_until}

    def list_ledger_accounts(self):
        return LEDGER_ACCOUNTS

    def get_report(self, name, *, period, **_kwargs):
        if name == "general_ledger":
            if self.ledger_override is not None:
                return self.ledger_override
            if period not in GENERAL_LEDGERS:
                raise AssertionError(f"no ledger fixture for period {period!r}")
            return GENERAL_LEDGERS[period]
        if name == "tax":
            # Mirrors the live constraint: only single months are accepted.
            if period not in Q2_TAX_REPORTS:
                raise AssertionError(f"tax report requested for non-month {period!r}")
            return Q2_TAX_REPORTS[period]
        raise AssertionError(f"unexpected report {name}")

    def list_documents(self, kind, **_kwargs):
        assert kind == "general_journal_document"
        return self.general_journals


class NoRoundingExactClient(FakeClient):
    """Stock-like chart without Afrondingsverschillen and an exact VAT position."""

    def list_ledger_accounts(self):
        return [item for item in LEDGER_ACCOUNTS if item["id"] != ROUNDING]

    def get_report(self, name, *, period, **_kwargs):
        if name == "general_ledger":
            return _general_ledger("100.00", "40.00")
        if name == "tax" and period == "202604":
            return _tax_report("100.00", "40.00", "0.00")
        raise AssertionError(f"unexpected report {name} for {period}")


class LedgerMovementParsingTests(unittest.TestCase):
    def test_extracts_debit_and_credit_turnover(self):
        movements = ledger_movements_from_report(
            Q2_GENERAL_LEDGER, [PAYABLE, RECEIVABLE]
        )
        self.assertEqual(movements[PAYABLE].net_credit, Decimal("5232.05"))
        self.assertEqual(movements[RECEIVABLE].net_debit, Decimal("808.00"))

    def test_absent_account_becomes_zero_movement(self):
        movements = ledger_movements_from_report(Q2_GENERAL_LEDGER, [SETTLEMENT])
        self.assertEqual(movements[SETTLEMENT].net_credit, Decimal("0.00"))

    def test_descends_into_nested_children(self):
        report = {
            "debit_sums": {
                "ledger_accounts": [
                    {
                        "ledger_account_id": "parent",
                        "value": "0",
                        "children": [{"ledger_account_id": RECEIVABLE, "value": "10.00"}],
                    }
                ]
            },
            "credit_sums": {"ledger_accounts": []},
        }
        movements = ledger_movements_from_report(report, [RECEIVABLE])
        self.assertEqual(movements[RECEIVABLE].net_debit, Decimal("10.00"))

    def test_reversals_net_against_the_same_account(self):
        report = {
            "debit_sums": {"ledger_accounts": [{"ledger_account_id": PAYABLE, "value": "10.50"}]},
            "credit_sums": {"ledger_accounts": [{"ledger_account_id": PAYABLE, "value": "5232.05"}]},
        }
        movements = ledger_movements_from_report(report, [PAYABLE])
        self.assertEqual(movements[PAYABLE].net_credit, Decimal("5221.55"))


class ReportedTotalsTests(unittest.TestCase):
    def test_sums_monthly_reports_into_payable_and_deductible(self):
        reported = reported_vat_totals(Q2_TAX_REPORTS.values())
        self.assertEqual(reported["payable"], Decimal("5225.75"))
        self.assertEqual(reported["deductible"], Decimal("801.70"))
        self.assertEqual(reported["net"], Decimal("4424.05"))


class MonthPeriodTests(unittest.TestCase):
    def test_quarter_splits_into_its_months(self):
        self.assertEqual(
            month_periods("20260401..20260630"), ["202604", "202605", "202606"]
        )

    def test_single_month_yields_one_period(self):
        self.assertEqual(month_periods("20260201..20260228"), ["202602"])

    def test_year_boundary_is_crossed(self):
        self.assertEqual(
            month_periods("20251201..20260131"), ["202512", "202601"]
        )

    def test_symbolic_period_is_refused(self):
        with self.assertRaises(MoneybirdError):
            month_periods("this_quarter")

    def test_partial_month_is_refused_rather_than_mis_summed(self):
        with self.assertRaises(MoneybirdError):
            month_periods("20260415..20260630")
        with self.assertRaises(MoneybirdError):
            month_periods("20260401..20260615")

    def test_reversed_range_is_refused(self):
        with self.assertRaises(MoneybirdError):
            month_periods("20260601..20260430")


class GrossVersusReportedTests(unittest.TestCase):
    def test_offsetting_reverse_charge_is_explained_not_flagged(self):
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("5232.05"),
            gross_deductible=Decimal("808.00"),
            reported_payable=Decimal("5225.75"),
            reported_deductible=Decimal("801.70"),
        )
        self.assertEqual(comparison["offsetting_amount"], "6.30")
        self.assertTrue(comparison["offsetting_explained"])
        self.assertEqual(comparison["net_unexplained"], "0.00")
        self.assertFalse(comparison["is_anomaly"])
        self.assertIn("btw verlegd", comparison["explanation"])

    def test_one_sided_excess_is_a_real_anomaly(self):
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("5232.05"),
            gross_deductible=Decimal("801.70"),
            reported_payable=Decimal("5225.75"),
            reported_deductible=Decimal("801.70"),
        )
        self.assertTrue(comparison["is_anomaly"])
        self.assertEqual(comparison["net_unexplained"], "6.30")

    def test_unequal_excess_reports_only_the_unexplained_remainder(self):
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("5237.05"),
            gross_deductible=Decimal("808.00"),
            reported_payable=Decimal("5225.75"),
            reported_deductible=Decimal("801.70"),
        )
        self.assertEqual(comparison["offsetting_amount"], "6.30")
        self.assertEqual(comparison["net_unexplained"], "5.00")
        self.assertTrue(comparison["is_anomaly"])

    def test_matching_totals_report_no_gap(self):
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("100.00"),
            gross_deductible=Decimal("40.00"),
            reported_payable=Decimal("100.00"),
            reported_deductible=Decimal("40.00"),
        )
        self.assertFalse(comparison["is_anomaly"])
        self.assertFalse(comparison["offsetting_explained"])


class AccountResolutionTests(unittest.TestCase):
    def test_resolves_the_four_roles_by_conventional_name(self):
        accounts = accounts_by_role()
        self.assertEqual(str(accounts["payable"]["id"]), PAYABLE)
        self.assertEqual(str(accounts["rounding"]["id"]), ROUNDING)

    def test_explicit_id_overrides_the_name_lookup(self):
        accounts = resolve_vat_accounts(
            LEDGER_ACCOUNTS, overrides={"rounding": SETTLEMENT}
        )
        self.assertEqual(str(accounts["rounding"]["id"]), SETTLEMENT)

    def test_unknown_override_id_is_rejected(self):
        with self.assertRaises(MoneybirdError):
            resolve_vat_accounts(LEDGER_ACCOUNTS, overrides={"payable": "999"})

    def test_missing_account_lists_candidates_instead_of_guessing(self):
        without_rounding = [
            item for item in LEDGER_ACCOUNTS if item["id"] != ROUNDING
        ]
        with self.assertRaises(MoneybirdError) as caught:
            resolve_vat_accounts(without_rounding)
        message = str(caught.exception)
        self.assertIn("Afrondingsverschillen", message)
        self.assertIn("rounding_ledger_account_id", message)
        self.assertIn("prepare_create_ledger_account", message)
        self.assertNotIn("16224 Te betalen btw", message)

    def test_rounding_candidates_prefer_semantically_adjacent_accounts(self):
        accounts = [
            item for item in LEDGER_ACCOUNTS if item["id"] != ROUNDING
        ] + [
            {"id": "1", "name": "Huisvestingskosten", "account_id": "45185", "account_type": "expenses"},
            {"id": "2", "name": "Verkoopkosten", "account_id": "45680", "account_type": "expenses"},
            {"id": "3", "name": "Vervoerskosten", "account_id": "45875", "account_type": "expenses"},
            {"id": "4", "name": "Koersverschillen", "account_id": "46500", "account_type": "other_income_expenses"},
        ]

        with self.assertRaises(MoneybirdError) as caught:
            resolve_vat_accounts(accounts)

        message = str(caught.exception)
        self.assertIn("46500 Koersverschillen", message)
        self.assertNotIn("Vervoerskosten", message)


class SettlementJournalTests(unittest.TestCase):
    def test_quarter_clears_gross_and_books_the_rounding_advantage(self):
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("5232.05"),
            receivable_movement=Decimal("808.00"),
            declared_amount=DECLARED_Q2,
            description="Btw-aangifte Q2 2026",
        )
        self.assertEqual(journal["net_position"], "4424.05")
        self.assertEqual(journal["rounding_difference"], "1.05")
        self.assertEqual(journal["total_debit"], journal["total_credit"])
        self.assertEqual(journal["total_debit"], "5232.05")

        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        self.assertEqual(by_account[PAYABLE]["debit"], "5232.05")
        self.assertEqual(by_account[RECEIVABLE]["credit"], "808.00")
        self.assertEqual(by_account[SETTLEMENT]["credit"], "4423.00")
        self.assertEqual(by_account[ROUNDING]["credit"], "1.05")

    def test_clearing_only_the_reported_amounts_would_strand_the_reverse_charge(self):
        # Guards the original defect: the tax-report figures leave 6.30 behind on
        # both accounts, so they must never be used as the clearing basis.
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("5225.75"),
            receivable_movement=Decimal("801.70"),
            declared_amount=DECLARED_Q2,
            description="",
        )
        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        stranded_payable = Decimal("5232.05") - Decimal(by_account[PAYABLE]["debit"])
        stranded_receivable = Decimal("808.00") - Decimal(by_account[RECEIVABLE]["credit"])
        self.assertEqual(stranded_payable, Decimal("6.30"))
        self.assertEqual(stranded_receivable, Decimal("6.30"))

    def test_refund_period_debits_the_settlement_account(self):
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("1000.00"),
            receivable_movement=Decimal("1221.00"),
            declared_amount=Decimal("-221.00"),
            description="",
        )
        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        self.assertEqual(journal["net_position"], "-221.00")
        self.assertEqual(by_account[SETTLEMENT]["debit"], "221.00")
        self.assertEqual(journal["rounding_difference"], "0.00")
        self.assertNotIn(ROUNDING, by_account)
        self.assertEqual(journal["total_debit"], journal["total_credit"])

    def test_rounding_against_the_taxpayer_debits_the_rounding_account(self):
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("5232.05"),
            receivable_movement=Decimal("808.00"),
            declared_amount=Decimal("4425.00"),
            description="",
        )
        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        self.assertEqual(journal["rounding_difference"], "-0.95")
        self.assertEqual(by_account[ROUNDING]["debit"], "0.95")
        self.assertEqual(journal["total_debit"], journal["total_credit"])

    def test_period_without_movement_is_refused(self):
        with self.assertRaises(MoneybirdError):
            build_vat_settlement_journal(
                accounts=accounts_by_role(),
                payable_movement=Decimal("0.00"),
                receivable_movement=Decimal("0.00"),
                declared_amount=Decimal("0.00"),
                description="",
            )


class ReverseChargeVariantTests(unittest.TestCase):
    """Other reverse-charge rubrieken must offset the same way as intra-EU 4b/5b."""

    def test_domestic_reverse_charge_offsets_like_intra_eu(self):
        # 'Btw verlegd binnenland' lands on the sales side (NL/1e) and is deducted
        # again on NL/5b, so it inflates both gross movements by the same amount.
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("5210.00"),
            gross_deductible=Decimal("1210.00"),
            reported_payable=Decimal("5000.00"),
            reported_deductible=Decimal("1000.00"),
        )
        self.assertEqual(comparison["offsetting_amount"], "210.00")
        self.assertFalse(comparison["is_anomaly"])

    def test_several_reverse_charge_rubrieken_offset_in_aggregate(self):
        # Intra-EU goods (4a), intra-EU services (4b) and domestic (1e) together.
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("9000.00") + Decimal("31.50"),
            gross_deductible=Decimal("2000.00") + Decimal("31.50"),
            reported_payable=Decimal("9000.00"),
            reported_deductible=Decimal("2000.00"),
        )
        self.assertEqual(comparison["offsetting_amount"], "31.50")
        self.assertFalse(comparison["is_anomaly"])

    def test_reverse_charge_reported_with_a_nonzero_tax_amount_still_offsets(self):
        # Some rubrieken report the tax rather than zero; the ledger then matches
        # the report exactly and there is simply no gap to explain.
        reported = reported_vat_totals(
            [
                {
                    "tax_rates": [
                        {"name": "21% btw", "report_reference": "NL/1a", "type": "sales_invoice", "tax": "1000.00"},
                        {"name": "Binnen EU 21% (btw verlegd)", "report_reference": "NL/4b", "type": "sales_invoice", "tax": "21.00"},
                        {"name": "Binnen EU 21% (btw verlegd)", "report_reference": "NL/5b", "type": "purchase_invoice", "tax": "21.00"},
                    ]
                }
            ]
        )
        self.assertEqual(reported["payable"], Decimal("1021.00"))
        self.assertEqual(reported["deductible"], Decimal("21.00"))
        comparison = compare_gross_to_reported(
            gross_payable=Decimal("1021.00"),
            gross_deductible=Decimal("21.00"),
            reported_payable=reported["payable"],
            reported_deductible=reported["deductible"],
        )
        self.assertFalse(comparison["is_anomaly"])
        self.assertFalse(comparison["offsetting_explained"])


class RefundAndRoundingTests(unittest.TestCase):
    def test_refund_with_rounding_in_the_taxpayers_favour(self):
        # A refund is rounded up in the taxpayer's favour, so more is received than
        # the exact net: the rounding line lands on the opposite side of a payment.
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("1000.00"),
            receivable_movement=Decimal("1220.40"),
            declared_amount=Decimal("-221.00"),
            description="",
        )
        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        self.assertEqual(journal["net_position"], "-220.40")
        self.assertEqual(journal["rounding_difference"], "0.60")
        self.assertEqual(by_account[SETTLEMENT]["debit"], "221.00")
        self.assertEqual(by_account[ROUNDING]["credit"], "0.60")
        self.assertEqual(journal["total_debit"], journal["total_credit"])

    def test_refund_smaller_than_the_exact_net_debits_rounding(self):
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("1000.00"),
            receivable_movement=Decimal("1221.00"),
            declared_amount=Decimal("-220.00"),
            description="",
        )
        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        self.assertEqual(journal["rounding_difference"], "-1.00")
        self.assertEqual(by_account[ROUNDING]["debit"], "1.00")
        self.assertEqual(journal["total_debit"], journal["total_credit"])

    def test_monthly_return_settles_a_single_month(self):
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("1250.00"),
            receivable_movement=Decimal("214.40"),
            declared_amount=Decimal("1035.00"),
            description="Btw-aangifte april 2026",
        )
        self.assertEqual(journal["net_position"], "1035.60")
        self.assertEqual(journal["rounding_difference"], "0.60")
        self.assertEqual(journal["total_debit"], journal["total_credit"])


class PartiallySettledPeriodTests(unittest.TestCase):
    def test_remaining_gross_movement_is_what_gets_cleared(self):
        # An earlier partial clearing left part of each balance behind. The journal
        # must clear the remainder, not the original turnover.
        movements = ledger_movements_from_report(
            {
                "debit_sums": {
                    "ledger_accounts": [
                        {"ledger_account_id": RECEIVABLE, "value": "808.00"},
                        {"ledger_account_id": PAYABLE, "value": "2000.00"},
                    ]
                },
                "credit_sums": {
                    "ledger_accounts": [
                        {"ledger_account_id": PAYABLE, "value": "5232.05"},
                        {"ledger_account_id": RECEIVABLE, "value": "300.00"},
                    ]
                },
            },
            [PAYABLE, RECEIVABLE],
        )
        self.assertEqual(movements[PAYABLE].net_credit, Decimal("3232.05"))
        self.assertEqual(movements[RECEIVABLE].net_debit, Decimal("508.00"))

        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=movements[PAYABLE].net_credit,
            receivable_movement=movements[RECEIVABLE].net_debit,
            declared_amount=Decimal("2723.00"),
            description="",
        )
        by_account = {entry["ledger_account_id"]: entry for entry in journal["entries"]}
        self.assertEqual(by_account[PAYABLE]["debit"], "3232.05")
        self.assertEqual(by_account[RECEIVABLE]["credit"], "508.00")
        self.assertEqual(journal["rounding_difference"], "1.05")

    def test_zero_net_with_offsetting_gross_is_still_settleable(self):
        # Reverse-charge-only period: the net is zero but both accounts carry
        # movement. Refusing on the net alone would strand it.
        movements = {
            PAYABLE: LedgerMovement(PAYABLE, Decimal("0.00"), Decimal("31.50")),
            RECEIVABLE: LedgerMovement(RECEIVABLE, Decimal("31.50"), Decimal("0.00")),
        }
        preflight = settlement_preflight(
            movements=movements,
            accounts=accounts_by_role(),
            existing_journals=[],
            reference="BTW-2026-Q3",
        )
        self.assertTrue(preflight["clear_to_prepare"])
        journal = build_vat_settlement_journal(
            accounts=accounts_by_role(),
            payable_movement=Decimal("31.50"),
            receivable_movement=Decimal("31.50"),
            declared_amount=Decimal("0.00"),
            description="",
        )
        self.assertEqual(journal["net_position"], "0.00")
        self.assertEqual(journal["rounding_difference"], "0.00")
        self.assertEqual(len(journal["entries"]), 2)
        self.assertEqual(journal["total_debit"], "31.50")


class SettlementPreflightTests(unittest.TestCase):
    def _movements(self, payable_credit="5232.05", receivable_debit="808.00"):
        return {
            PAYABLE: LedgerMovement(PAYABLE, Decimal("0.00"), Decimal(payable_credit)),
            RECEIVABLE: LedgerMovement(RECEIVABLE, Decimal(receivable_debit), Decimal("0.00")),
        }

    def test_clean_period_is_clear_to_prepare(self):
        preflight = settlement_preflight(
            movements=self._movements(),
            accounts=accounts_by_role(),
            existing_journals=[{"id": "1", "reference": "ACT-123", "date": "2026-03-16"}],
            reference="BTW-2026-Q2",
        )
        self.assertTrue(preflight["clear_to_prepare"])
        self.assertEqual(preflight["gross_payable_movement"], "5232.05")

    def test_existing_reference_blocks_a_second_settlement(self):
        preflight = settlement_preflight(
            movements=self._movements(),
            accounts=accounts_by_role(),
            existing_journals=[{"id": "9", "reference": "btw-2026-q2", "date": "2026-06-30"}],
            reference="BTW-2026-Q2",
        )
        self.assertFalse(preflight["clear_to_prepare"])
        self.assertEqual(len(preflight["existing_reference_matches"]), 1)

    def test_period_and_vat_lines_block_even_under_a_different_reference(self):
        preflight = settlement_preflight(
            movements=self._movements(),
            accounts=accounts_by_role(),
            existing_journals=[
                _q2_settlement_journal(reference="something entirely different")
            ],
            reference="BTW-2026-Q2-nogmaals",
            period="20260401..20260630",
        )

        self.assertFalse(preflight["clear_to_prepare"])
        self.assertEqual(
            preflight["existing_period_settlement_matches"][0]["id"],
            "journal-existing",
        )
        self.assertIn(
            "Changing the journal reference",
            " ".join(preflight["blocking_findings"]),
        )

    def test_single_account_correction_is_not_mislabeled_as_a_settlement(self):
        correction = {
            "id": "correction-1",
            "reference": "VAT correction",
            "date": "2026-06-30",
            "general_journal_document_entries": [
                {
                    "ledger_account_id": PAYABLE,
                    "debit": "5.00",
                    "credit": "0.00",
                }
            ],
        }
        self.assertEqual(
            find_vat_settlement_journals(
                [correction],
                accounts=accounts_by_role(),
                period="20260401..20260630",
            ),
            [],
        )

    def test_period_without_gross_movement_blocks(self):
        preflight = settlement_preflight(
            movements=self._movements("0.00", "0.00"),
            accounts=accounts_by_role(),
            existing_journals=[],
            reference="BTW-2026-Q2",
        )
        self.assertFalse(preflight["clear_to_prepare"])

    def test_locked_period_blocks_before_any_write(self):
        preflight = settlement_preflight(
            movements=self._movements(),
            accounts=accounts_by_role(),
            existing_journals=[],
            reference="BTW-2026-Q2",
            journal_date="2026-06-30",
            period_locked_until="2026-06-30",
        )
        self.assertFalse(preflight["clear_to_prepare"])
        self.assertIn("locked", " ".join(preflight["blocking_findings"]))

    def test_journal_after_the_lock_date_is_allowed(self):
        preflight = settlement_preflight(
            movements=self._movements(),
            accounts=accounts_by_role(),
            existing_journals=[],
            reference="BTW-2026-Q2",
            journal_date="2026-06-30",
            period_locked_until="2026-03-31",
        )
        self.assertTrue(preflight["clear_to_prepare"])
        self.assertEqual(preflight["period_locked_until"], "2026-03-31")

    def test_unlocked_administration_reports_no_lock(self):
        preflight = settlement_preflight(
            movements=self._movements(),
            accounts=accounts_by_role(),
            existing_journals=[],
            reference="BTW-2026-Q2",
            journal_date="2026-06-30",
            period_locked_until="",
        )
        self.assertTrue(preflight["clear_to_prepare"])
        self.assertEqual(preflight["period_locked_until"], "")


class AnalyzeVatSettlementToolTests(unittest.TestCase):
    def test_analysis_works_without_a_rounding_account(self):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        client = NoRoundingExactClient()
        with mock.patch.object(_context, "get_client", return_value=client):
            result = ledger_tools.analyze_vat_settlement(
                period="20260401..20260430"
            )

        self.assertEqual(result["gross_movements"]["net_position"], "60.00")
        self.assertNotIn("rounding", result["accounts"])

    def test_invalid_period_is_rejected_before_ledger_api_calls(self):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        class CallCountingClient:
            list_ledger_accounts_calls = 0

            def list_ledger_accounts(self):
                self.list_ledger_accounts_calls += 1
                return LEDGER_ACCOUNTS

        client = CallCountingClient()
        with mock.patch.object(_context, "get_client", return_value=client):
            with self.assertRaisesRegex(MoneybirdError, "explicit range"):
                ledger_tools.analyze_vat_settlement(period="this_year")

        self.assertEqual(client.list_ledger_accounts_calls, 0)

    def test_already_settled_period_is_reconstructed_not_called_anomalous(self):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        client = FakeClient(
            general_journals=[_q2_settlement_journal()],
            ledger_override=_general_ledger("0.00", "0.00"),
        )
        with mock.patch.object(_context, "get_client", return_value=client):
            result = ledger_tools.analyze_vat_settlement(
                period="20260401..20260630"
            )

        self.assertTrue(result["settlement_status"]["already_settled"])
        self.assertFalse(result["gross_vs_reported"]["is_anomaly"])
        self.assertEqual(
            result["gross_movements"]["basis"],
            "reconstructed_before_existing_settlement_journals",
        )
        self.assertEqual(
            result["gross_movements"]["current_period_net_after_journals"][
                "payable_net_credit"
            ],
            "0.00",
        )
        self.assertIn("Do not prepare another", result["next_step"])


class PrepareVatSettlementToolTests(unittest.TestCase):
    def setUp(self) -> None:
        from moneybird_mcp.credentials import set_active_administration_id

        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_vat_tool_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id("vat-admin")

    def tearDown(self) -> None:
        from moneybird_mcp.credentials import set_active_administration_id

        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    def _prepare(self, *, client=None, **overrides):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        kwargs = {
            "period": "20260401..20260630",
            "reference": "BTW-2026-Q2",
            "date": "2026-06-30",
            "declared_amount": "4423.00",
            "description": "Btw-aangifte Q2 2026 (extern ingediend)",
        }
        kwargs.update(overrides)
        client = client or FakeClient()
        with mock.patch.object(_context, "get_client", return_value=client):
            return ledger_tools.prepare_vat_settlement_journal(**kwargs)

    def test_locked_administration_refuses_before_any_write(self):
        client = FakeClient(period_locked_until="2026-06-30")
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(client=client)
        self.assertIn("locked", str(caught.exception))

    def test_already_settled_period_refuses_before_any_write(self):
        client = FakeClient(
            general_journals=[_q2_settlement_journal(reference="different text")]
        )
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(client=client, reference="BTW-2026-Q2-nogmaals")
        self.assertIn("already", str(caught.exception))

    def test_monthly_period_prepares_end_to_end(self):
        staged = self._prepare(
            period="20260401..20260430",
            reference="BTW-2026-04",
            date="",
            declared_amount="1035.00",
        )
        settlement = staged["preview"]["vat_settlement"]
        self.assertEqual(settlement["gross_payable_cleared"], "1252.10")
        self.assertEqual(settlement["net_position"], "1035.60")
        self.assertEqual(settlement["rounding_difference"], "0.60")
        self.assertEqual(staged["preview"]["date"], "2026-04-30")
        self.assertEqual(
            staged["preview"]["total_debit"], staged["preview"]["total_credit"]
        )

    def test_exact_settlement_does_not_require_a_rounding_account(self):
        staged = self._prepare(
            client=NoRoundingExactClient(),
            period="20260401..20260430",
            reference="BTW-2026-04-exact",
            date="",
            declared_amount="60",
        )

        settlement = staged["preview"]["vat_settlement"]
        self.assertEqual(settlement["rounding_difference"], "0.00")
        self.assertNotIn("rounding", settlement["accounts"])
        self.assertEqual(len(staged["preview"]["entries"]), 3)

    def test_nonzero_rounding_names_the_real_override_and_creation_tool(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(
                client=NoRoundingExactClient(),
                period="20260401..20260430",
                reference="BTW-2026-04-rounded",
                date="",
                declared_amount="59",
            )

        message = str(caught.exception)
        self.assertIn("rounding_ledger_account_id", message)
        self.assertIn("prepare_create_ledger_account", message)

    def test_staged_payload_matches_the_quarter_settlement(self):
        staged = self._prepare()
        preview = staged["preview"]
        self.assertEqual(preview["total_debit"], preview["total_credit"])
        self.assertEqual(preview["vat_settlement"]["gross_payable_cleared"], "5232.05")
        self.assertEqual(preview["vat_settlement"]["gross_receivable_cleared"], "808.00")
        self.assertEqual(preview["vat_settlement"]["net_position"], "4424.05")
        self.assertEqual(preview["vat_settlement"]["declared_amount"], "4423.00")
        self.assertEqual(preview["vat_settlement"]["rounding_difference"], "1.05")

    def test_preview_explains_the_reverse_charge_instead_of_flagging_it(self):
        preview = self._prepare()["preview"]
        self.assertFalse(preview["gross_vs_reported"]["is_anomaly"])
        self.assertEqual(preview["gross_vs_reported"]["offsetting_amount"], "6.30")

    def test_declared_amount_is_required_and_never_derived(self):
        with self.assertRaises(MoneybirdError):
            self._prepare(declared_amount="  ")

    def test_description_rides_on_the_lines_not_the_dropped_header_field(self):
        # Moneybird stores no header description on a general journal document, so
        # sending one fails the post-write verifier on every settlement.
        staged = self._prepare()
        document = staged["payload"]["general_journal_document"]
        self.assertNotIn("description", document)
        line_descriptions = [
            entry["description"]
            for entry in document["general_journal_document_entries_attributes"].values()
        ]
        self.assertEqual(len(line_descriptions), 4)
        for text in line_descriptions:
            self.assertTrue(text.startswith("Btw-aangifte Q2 2026 (extern ingediend) - "))

    def test_stages_under_its_own_contract_covered_action(self):
        from moneybird_mcp.tools.approvals import APPROVAL_EXECUTORS
        from moneybird_mcp.write_contracts import WRITE_SPECS

        staged = self._prepare()
        # A dedicated action, not the generic journal one: only its own executor
        # re-proves the VAT preconditions between approval and dispatch.
        self.assertEqual(staged["action"], "settle_vat_period")
        self.assertIn("settle_vat_period", WRITE_SPECS)
        self.assertIn("settle_vat_period", APPROVAL_EXECUTORS)

    def test_fingerprint_identifies_the_period_not_the_wording(self):
        # Duplicate suppression has to survive a second attempt under a different
        # reference, date or description; the settled period is the identity.
        first = self._prepare()
        second = self._prepare(
            reference="BTW-Q2-again",
            description="andere omschrijving",
            date="",
        )
        self.assertEqual(
            first["payload"]["fingerprint"], second["payload"]["fingerprint"]
        )

    def test_snapshot_carries_the_state_execution_must_reprove(self):
        payload = self._prepare()["payload"]
        snapshot = payload["snapshot"]
        self.assertEqual(snapshot["period"], "20260401..20260630")
        self.assertEqual(snapshot["gross_payable"], "5232.05")
        self.assertEqual(snapshot["gross_receivable"], "808.00")
        self.assertIn("period_locked_until", snapshot)
        self.assertTrue(payload["verify_period_cleared"])


class DeclaredAmountGuardTests(PrepareVatSettlementToolTests):
    """A balanced journal absorbs any mistake into the rounding line, so the
    amount has to be challenged before it is ever staged."""

    def test_amount_with_cents_is_refused(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(declared_amount="4423.47")
        self.assertIn("whole euros", str(caught.exception))

    def test_implausible_amount_is_refused_by_a_derived_bound(self):
        # A typo of 60000 against a net of 4424.05 must not be written off as
        # rounding. The bound comes from the rubriek count, not a fixed tolerance.
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(declared_amount="60000")
        message = str(caught.exception)
        self.assertIn("exceeds what rounding", message)
        self.assertIn("rubrieken", message)

    def test_plausible_amount_records_the_rounding_direction(self):
        check = self._prepare()["preview"]["declared_amount_check"]
        self.assertTrue(check["acceptable"])
        self.assertTrue(check["rounding_in_taxpayers_favour"])
        self.assertEqual(check["rounding_difference"], "1.05")

    def test_rounding_against_the_taxpayer_is_flagged_but_allowed(self):
        check = self._prepare(declared_amount="4425")["preview"]["declared_amount_check"]
        self.assertTrue(check["acceptable"])
        self.assertFalse(check["rounding_in_taxpayers_favour"])


class SettlementDateGuardTests(PrepareVatSettlementToolTests):
    def test_date_defaults_to_the_period_close(self):
        staged = self._prepare(date="")
        self.assertEqual(staged["preview"]["date"], "2026-06-30")
        self.assertEqual(staged["preview"]["period_end"], "2026-06-30")

    def test_date_outside_the_period_is_refused(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(date="2026-07-01")
        self.assertIn("period closes on 2026-06-30", str(caught.exception))

    def test_date_outside_the_period_needs_an_explicit_override(self):
        staged = self._prepare(date="2026-07-01", allow_date_outside_period=True)
        self.assertEqual(staged["preview"]["date"], "2026-07-01")
        self.assertTrue(
            staged["preview"]["preflight"]["overrides"]["allow_date_outside_period"]
        )
        # The period cannot be proven clear afterwards from outside its own window.
        self.assertFalse(staged["payload"]["verify_period_cleared"])

    def test_a_later_date_cannot_slip_past_a_locked_period(self):
        # The lock is checked against the period close as well, so dating the
        # journal into the next quarter does not settle a locked Q2.
        client = FakeClient(period_locked_until="2026-06-30")
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(
                client=client, date="2026-07-01", allow_date_outside_period=True
            )
        self.assertIn("locked", str(caught.exception))


class UnexplainedDifferenceGuardTests(PrepareVatSettlementToolTests):
    def _mismatched_client(self):
        # Payable short of the report by 5.00: a missing or misfiled mutation.
        return FakeClient(ledger_override=_general_ledger("5220.75", "808.00"))

    def test_unexplained_difference_blocks_preparation(self):
        with self.assertRaises(MoneybirdError) as caught:
            self._prepare(client=self._mismatched_client())
        message = str(caught.exception)
        self.assertIn("do not reconcile", message)
        self.assertEqual(message.count("Gross movements do not reconcile"), 1)

    def test_override_records_the_deliberate_exception(self):
        staged = self._prepare(
            client=self._mismatched_client(),
            declared_amount="4412",
            allow_unexplained_difference=True,
        )
        preflight = staged["preview"]["preflight"]
        self.assertTrue(preflight["overrides"]["allow_unexplained_difference"])
        self.assertTrue(staged["preview"]["gross_vs_reported"]["is_anomaly"])


class ExecuteVatSettlementTests(PrepareVatSettlementToolTests):
    """Approval and dispatch are separated in time; the executor must re-prove."""

    def _execute(self, client, approval_id):
        from moneybird_mcp.tools import _context
        from moneybird_mcp.tools import ledger as ledger_tools

        with mock.patch.object(_context, "get_client", return_value=client):
            return ledger_tools.vat_settlement_journal_from_approval(approval_id)

    def _settling_client(self, **kwargs):
        class SettlingClient(FakeClient):
            def __init__(self, **inner):
                super().__init__(**inner)
                self.cleared = False

            def create_general_journal_document(self, payload):
                self.created.append(payload)
                self.cleared = True
                return {"id": "journal-1"}

            def get_document(self, _kind, _document_id):
                sent = self.created[-1]
                return {
                    "id": "journal-1",
                    "reference": sent["reference"],
                    "date": sent["date"],
                    "general_journal_document_entries": list(
                        sent["general_journal_document_entries_attributes"].values()
                    ),
                }

            def get_report(self, name, *, period, **inner):
                if name == "general_ledger" and self.cleared:
                    # After settling, the period's VAT accounts net to zero.
                    return _general_ledger("0.00", "0.00")
                return super().get_report(name, period=period, **inner)

        return SettlingClient(**kwargs)

    def test_successful_settlement_verifies_the_period_cleared(self):
        client = self._settling_client()
        staged = self._prepare(client=client)
        result = self._execute(client, staged["approval_id"])

        self.assertEqual(result["status"], "created")
        verification = result["verification"]
        self.assertTrue(verification["preconditions_reproved_before_dispatch"])
        self.assertTrue(verification["period_vat_accounts_cleared"])
        self.assertTrue(verification["fully_verified"])

    def test_movement_drift_since_approval_aborts_before_dispatch(self):
        client = self._settling_client()
        staged = self._prepare(client=client)
        # A new VAT mutation lands between approval and execution.
        client.ledger_override = _general_ledger("5260.00", "808.00")

        with self.assertRaises(MoneybirdError) as caught:
            self._execute(client, staged["approval_id"])
        self.assertIn("changed since approval", str(caught.exception))
        self.assertEqual(client.created, [])

    def test_a_settlement_appearing_since_approval_aborts_before_dispatch(self):
        client = self._settling_client()
        staged = self._prepare(client=client)
        client.general_journals = [
            _q2_settlement_journal(reference="different text")
        ]

        with self.assertRaises(MoneybirdError) as caught:
            self._execute(client, staged["approval_id"])
        self.assertIn("settlement-like journal", str(caught.exception))
        self.assertEqual(client.created, [])

    def test_a_new_lock_since_approval_aborts_before_dispatch(self):
        client = self._settling_client()
        staged = self._prepare(client=client)
        client.period_locked_until = "2026-06-30"

        with self.assertRaises(MoneybirdError) as caught:
            self._execute(client, staged["approval_id"])
        self.assertIn("lock changed", str(caught.exception))
        self.assertEqual(client.created, [])

    def test_a_period_that_stays_dirty_is_not_reported_as_verified(self):
        class StubbornClient(FakeClient):
            def create_general_journal_document(self, payload):
                self.created.append(payload)
                return {"id": "journal-1"}

            def get_document(self, _kind, _document_id):
                sent = self.created[-1]
                return {
                    "id": "journal-1",
                    "reference": sent["reference"],
                    "date": sent["date"],
                    "general_journal_document_entries": list(
                        sent["general_journal_document_entries_attributes"].values()
                    ),
                }

        client = StubbornClient()
        staged = self._prepare(client=client)
        result = self._execute(client, staged["approval_id"])

        self.assertEqual(result["status"], "completed_with_verification_errors")
        self.assertFalse(result["verification"]["period_vat_accounts_cleared"])
        self.assertFalse(result["verification"]["fully_verified"])


if __name__ == "__main__":
    unittest.main()
