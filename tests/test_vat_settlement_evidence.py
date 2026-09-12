"""Regression tests for ledger-derived VAT settlement evidence.

A general journal is not the only thing that clears a VAT period. Moneybird
settles a btw-aangifte with its own ``VatDocument``, which never appears in the
general_journal_documents collection, so a detector that only reads that
collection reports a settled quarter as unsettled and invites a second
settlement over the same movements. The anchor figures here are taken from a real
administration where exactly that happened.

Three questions are kept apart throughout, because conflating them is what made
the original defect possible:

* was the VAT ledger cleared (this is what may block a write);
* what exactly was cleared (may be withheld when unprovable);
* was the return filed, and was the tax paid (neither is knowable here).
"""
import unittest
from decimal import Decimal

from test_vat_settlement import (
    PAYABLE,
    RECEIVABLE,
    ROUNDING,
    SETTLEMENT,
    accounts_by_role,
)

from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.vat_settlement import (
    SIGN_POSITIVE_IS_CREDIT,
    SIGN_POSITIVE_IS_DEBIT,
    LedgerMovement,
    compare_gross_to_reported,
    find_ledger_settlement_occurrences,
    find_vat_rounding_adjustments,
    find_vat_settlement_journals,
    prove_sign_mapping,
    settlement_preflight,
)

Q2 = "20260401..20260630"


def _movement(account_id, debit="0.00", credit="0.00"):
    return LedgerMovement(
        ledger_account_id=account_id,
        debit=Decimal(debit),
        credit=Decimal(credit),
    )


def _row(account_id, amount, *, document_id, document_type="VatDocument", date="2026-06-30"):
    return {
        "id": f"{document_id}-{account_id}",
        "date": date,
        "document_type": document_type,
        "document_id": document_id,
        "ledger_account_id": account_id,
        "amount": amount,
    }


def _vat_document_rows(document_id="vat-doc-q2", date="2026-06-30"):
    """A complete Moneybird VatDocument settlement, in the observed sign convention.

    Positive is a credit on these liability-typed accounts: the input-VAT account
    is credited to clear it, the output-VAT account debited, and the settlement
    account debited for the net.
    """

    return [
        _row(RECEIVABLE, "808.00", document_id=document_id, date=date),
        _row(PAYABLE, "-5232.05", document_id=document_id, date=date),
        _row(SETTLEMENT, "-4424.05", document_id=document_id, date=date),
    ]


class SignMappingProofTests(unittest.TestCase):
    """The report's sign convention is derived from totals, never assumed."""

    def test_positive_total_matching_credit_proves_credit(self):
        rows = [_row(RECEIVABLE, "808.00", document_id="a")]
        mapping = prove_sign_mapping(rows, _movement(RECEIVABLE, credit="808.00"))
        self.assertTrue(mapping.proven)
        self.assertEqual(mapping.positive_is, SIGN_POSITIVE_IS_CREDIT)
        self.assertEqual(
            mapping.debit_credit(Decimal("808.00")),
            (Decimal("0.00"), Decimal("808.00")),
        )

    def test_positive_total_matching_debit_proves_debit(self):
        # A current_assets account carries the opposite convention live, which is
        # exactly why this is proven per account rather than hard-coded once.
        rows = [_row(RECEIVABLE, "322.44", document_id="a")]
        mapping = prove_sign_mapping(rows, _movement(RECEIVABLE, debit="322.44"))
        self.assertTrue(mapping.proven)
        self.assertEqual(mapping.positive_is, SIGN_POSITIVE_IS_DEBIT)
        self.assertEqual(
            mapping.debit_credit(Decimal("322.44")),
            (Decimal("322.44"), Decimal("0.00")),
        )

    def test_totals_that_match_neither_side_are_unproven(self):
        rows = [_row(RECEIVABLE, "100.00", document_id="a")]
        mapping = prove_sign_mapping(rows, _movement(RECEIVABLE, credit="808.00"))
        self.assertFalse(mapping.proven)
        self.assertIn("match neither", mapping.reason)
        with self.assertRaises(MoneybirdError):
            mapping.debit_credit(Decimal("100.00"))

    def test_symmetrical_totals_are_refused_rather_than_picked(self):
        rows = [
            _row(RECEIVABLE, "50.00", document_id="a"),
            _row(RECEIVABLE, "-50.00", document_id="b"),
        ]
        mapping = prove_sign_mapping(
            rows, _movement(RECEIVABLE, debit="50.00", credit="50.00")
        )
        self.assertFalse(mapping.proven)
        self.assertIn("cannot", mapping.reason)

    def test_no_rows_and_no_movement_is_trivially_proven(self):
        mapping = prove_sign_mapping([], _movement(RECEIVABLE))
        self.assertTrue(mapping.proven)

    def test_no_rows_while_the_ledger_moved_is_a_gap_not_a_proof(self):
        mapping = prove_sign_mapping([], _movement(RECEIVABLE, debit="808.00"))
        self.assertFalse(mapping.proven)
        self.assertIn("no journal-entry rows", mapping.reason)


class LedgerSettlementOccurrenceTests(unittest.TestCase):
    def setUp(self):
        self.accounts = accounts_by_role()
        self.mappings = {
            "payable": prove_sign_mapping(
                [_row(PAYABLE, "-5232.05", document_id="x")],
                _movement(PAYABLE, debit="5232.05"),
            ),
            "receivable": prove_sign_mapping(
                [_row(RECEIVABLE, "808.00", document_id="x")],
                _movement(RECEIVABLE, credit="808.00"),
            ),
            "settlement": prove_sign_mapping(
                [_row(SETTLEMENT, "-4424.05", document_id="x")],
                _movement(SETTLEMENT, debit="4424.05"),
            ),
        }

    def _occurrences(self, rows, **kwargs):
        by_role = {"payable": [], "receivable": [], "settlement": []}
        role_of = {PAYABLE: "payable", RECEIVABLE: "receivable", SETTLEMENT: "settlement"}
        for row in rows:
            by_role[role_of[row["ledger_account_id"]]].append(row)
        return find_ledger_settlement_occurrences(
            entry_rows_by_role=by_role,
            accounts=self.accounts,
            sign_mappings=kwargs.pop("sign_mappings", self.mappings),
            **kwargs,
        )

    def test_genuine_vat_document_is_a_settlement_occurrence(self):
        found = self._occurrences(_vat_document_rows())
        self.assertEqual(len(found), 1)
        occurrence = found[0]
        self.assertEqual(occurrence["document_type"], "VatDocument")
        self.assertEqual(occurrence["document_id"], "vat-doc-q2")
        self.assertEqual(
            occurrence["touched_roles"], ["payable", "receivable", "settlement"]
        )
        self.assertTrue(occurrence["amounts_reconstructed"])
        self.assertEqual(occurrence["payable_restore"], "5232.05")
        self.assertEqual(occurrence["receivable_restore"], "808.00")
        self.assertEqual(occurrence["settlement_amount"], "4424.05")

    def test_settlement_account_only_is_not_a_clearing(self):
        # VatDocument is also a documented link_booking booking_type, so a bank
        # payment allocated to a VAT return shows up here while clearing nothing.
        found = self._occurrences(
            [_row(SETTLEMENT, "4424.05", document_id="payment-alloc")]
        )
        self.assertEqual(found, [])

    def test_single_vat_account_correction_is_not_a_settlement(self):
        found = self._occurrences(
            [_row(RECEIVABLE, "-3.26", document_id="rounding-fix")]
        )
        self.assertEqual(found, [])

    def test_a_reverse_charge_purchase_invoice_is_not_a_settlement(self):
        # btw verlegd books input VAT and output VAT for the same amount, so every
        # such invoice touches both gross accounts. Live-verified: treating that as
        # a settlement produced 7 false positives in one quarter and 13 in the next.
        found = self._occurrences(
            [
                _row(RECEIVABLE, "-3748.02", document_id="import-invoice", document_type="Document"),
                _row(PAYABLE, "3748.02", document_id="import-invoice", document_type="Document"),
            ]
        )
        self.assertEqual(found, [])

    def test_many_reverse_charge_invoices_stay_out_of_the_evidence(self):
        rows = []
        for index in range(13):
            rows.append(
                _row(RECEIVABLE, "-10.00", document_id=f"rc-{index}", document_type="Document")
            )
            rows.append(
                _row(PAYABLE, "10.00", document_id=f"rc-{index}", document_type="Document")
            )
        self.assertEqual(self._occurrences(rows), [])

    def test_a_clearing_direction_without_the_settlement_account_still_counts(self):
        # Both gross accounts moved back towards zero, which no accrual does.
        found = self._occurrences(
            [
                _row(RECEIVABLE, "808.00", document_id="zero-net-settlement"),
                _row(PAYABLE, "-808.00", document_id="zero-net-settlement"),
            ]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(
            found[0]["matched_rule"],
            "both_gross_accounts_in_the_clearing_direction",
        )

    def test_an_unproven_sign_cannot_promote_a_reverse_charge_invoice(self):
        broken = dict(self.mappings)
        broken["payable"] = prove_sign_mapping(
            [_row(PAYABLE, "1.00", document_id="x")],
            _movement(PAYABLE, debit="5232.05"),
        )
        found = self._occurrences(
            [
                _row(RECEIVABLE, "-3748.02", document_id="import-invoice", document_type="Document"),
                _row(PAYABLE, "3748.02", document_id="import-invoice", document_type="Document"),
            ],
            sign_mappings=broken,
        )
        self.assertEqual(found, [])

    def test_one_gross_account_plus_settlement_counts(self):
        found = self._occurrences(
            [
                _row(RECEIVABLE, "808.00", document_id="vd"),
                _row(SETTLEMENT, "-808.00", document_id="vd"),
            ]
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["touched_roles"], ["receivable", "settlement"])

    def test_both_gross_accounts_without_settlement_counts(self):
        found = self._occurrences(
            [
                _row(RECEIVABLE, "808.00", document_id="vd"),
                _row(PAYABLE, "-808.00", document_id="vd"),
            ]
        )
        self.assertEqual(len(found), 1)

    def test_multiple_occurrences_are_all_reported(self):
        rows = _vat_document_rows("vat-doc-q2-original") + _vat_document_rows(
            "vat-doc-q2-suppletie"
        )
        found = self._occurrences(rows)
        self.assertEqual(len(found), 2)
        self.assertEqual(
            sorted(item["document_id"] for item in found),
            ["vat-doc-q2-original", "vat-doc-q2-suppletie"],
        )

    def test_general_journal_ids_are_excluded_so_nothing_counts_twice(self):
        rows = _vat_document_rows("journal-existing", date="2026-06-30")
        found = self._occurrences(rows, exclude_document_ids=["journal-existing"])
        self.assertEqual(found, [])

    def test_detection_survives_an_unproven_sign_but_withholds_amounts(self):
        broken = dict(self.mappings)
        broken["receivable"] = prove_sign_mapping(
            [_row(RECEIVABLE, "1.00", document_id="x")],
            _movement(RECEIVABLE, credit="808.00"),
        )
        found = self._occurrences(_vat_document_rows(), sign_mappings=broken)
        self.assertEqual(len(found), 1, "detection must not depend on the sign proof")
        self.assertFalse(found[0]["amounts_reconstructed"])
        self.assertIsNone(found[0]["payable_restore"])
        self.assertIsNone(found[0]["receivable_restore"])
        self.assertEqual(found[0]["unproven_roles"], ["receivable"])


class RoundingAdjustmentTests(unittest.TestCase):
    """The €3.26 Q4-2025 case: a real correction, explained rather than ignored."""

    def setUp(self):
        self.accounts = accounts_by_role()

    def _journal(self, entries, *, journal_id="round-fix", date="2025-12-31"):
        return {
            "id": journal_id,
            "reference": "Correctie verschil btw K4-2025",
            "date": date,
            "general_journal_document_entries": entries,
        }

    def test_vat_against_rounding_is_an_explained_adjustment(self):
        found = find_vat_rounding_adjustments(
            [
                self._journal(
                    [
                        {"ledger_account_id": RECEIVABLE, "debit": "3.26", "credit": "0.00"},
                        {"ledger_account_id": ROUNDING, "debit": "0.00", "credit": "3.26"},
                    ]
                )
            ],
            accounts=self.accounts,
            period="20251001..20251231",
        )
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["receivable_adjustment"], "3.26")
        self.assertEqual(found[0]["payable_adjustment"], "0.00")
        self.assertEqual(found[0]["category"], "vat_rounding_correction")
        self.assertEqual(found[0]["document_id"], "round-fix")

    def test_rounding_without_a_vat_account_is_ignored(self):
        # A cost write-off against the rounding account is none of VAT's business.
        found = find_vat_rounding_adjustments(
            [
                self._journal(
                    [
                        {"ledger_account_id": "999", "debit": "12.00", "credit": "0.00"},
                        {"ledger_account_id": ROUNDING, "debit": "0.00", "credit": "12.00"},
                    ]
                )
            ],
            accounts=self.accounts,
            period="20251001..20251231",
        )
        self.assertEqual(found, [])

    def test_absent_rounding_account_yields_no_adjustments(self):
        accounts = {
            role: value
            for role, value in self.accounts.items()
            if role != "rounding"
        }
        found = find_vat_rounding_adjustments(
            [
                self._journal(
                    [
                        {"ledger_account_id": RECEIVABLE, "debit": "3.26", "credit": "0.00"},
                        {"ledger_account_id": ROUNDING, "debit": "0.00", "credit": "3.26"},
                    ]
                )
            ],
            accounts=accounts,
            period="20251001..20251231",
        )
        self.assertEqual(found, [])

    def test_adjustment_outside_the_period_is_not_counted(self):
        found = find_vat_rounding_adjustments(
            [
                self._journal(
                    [
                        {"ledger_account_id": RECEIVABLE, "debit": "3.26", "credit": "0.00"},
                        {"ledger_account_id": ROUNDING, "debit": "0.00", "credit": "3.26"},
                    ],
                    date="2026-01-31",
                )
            ],
            accounts=self.accounts,
            period="20251001..20251231",
        )
        self.assertEqual(found, [])

    def test_a_settlement_journals_own_rounding_line_is_not_double_counted(self):
        settlement = self._journal(
            [
                {"ledger_account_id": PAYABLE, "debit": "5232.05", "credit": "0.00"},
                {"ledger_account_id": RECEIVABLE, "debit": "0.00", "credit": "808.00"},
                {"ledger_account_id": SETTLEMENT, "debit": "0.00", "credit": "4423.00"},
                {"ledger_account_id": ROUNDING, "debit": "0.00", "credit": "1.05"},
            ],
            journal_id="journal-existing",
            date="2026-06-30",
        )
        settlements = find_vat_settlement_journals(
            [settlement], accounts=self.accounts, period=Q2
        )
        self.assertEqual(len(settlements), 1)
        found = find_vat_rounding_adjustments(
            [settlement],
            accounts=self.accounts,
            period=Q2,
            exclude_document_ids=[item["id"] for item in settlements],
        )
        self.assertEqual(found, [])


class ExplainedDiscrepancyArithmeticTests(unittest.TestCase):
    """discrepancy - explained = residual, with the residual still able to fire."""

    def _compare(self, explained_deductible="0.00"):
        return compare_gross_to_reported(
            gross_payable=Decimal("11.84"),
            gross_deductible=Decimal("97.02"),
            reported_payable=Decimal("0.00"),
            reported_deductible=Decimal("81.92"),
            explained_deductible=Decimal(explained_deductible),
            explained_adjustments=(
                [{"document_id": "round-fix", "receivable_adjustment": explained_deductible}]
                if Decimal(explained_deductible)
                else []
            ),
        )

    def test_unexplained_case_matches_the_live_q4_2025_figures(self):
        comparison = self._compare()
        self.assertEqual(comparison["offsetting_amount"], "11.84")
        self.assertEqual(comparison["unexplained_deductible"], "3.26")
        self.assertEqual(comparison["residual_deductible"], "3.26")
        self.assertTrue(comparison["is_anomaly"])

    def test_fully_explained_q4_2025_leaves_no_anomaly(self):
        comparison = self._compare("3.26")
        self.assertEqual(comparison["unexplained_deductible"], "3.26")
        self.assertEqual(comparison["explained_deductible_adjustment"], "3.26")
        self.assertEqual(comparison["residual_deductible"], "0.00")
        self.assertFalse(comparison["is_anomaly"])
        self.assertIn("fully explained", comparison["explanation"])
        self.assertEqual(len(comparison["explained_adjustments"]), 1)

    def test_partially_explained_still_reports_its_residual(self):
        comparison = self._compare("1.00")
        self.assertEqual(comparison["explained_deductible_adjustment"], "1.00")
        self.assertEqual(comparison["residual_deductible"], "2.26")
        self.assertTrue(comparison["is_anomaly"])
        self.assertIn("2.26", comparison["explanation"])

    def test_over_explaining_does_not_silently_cancel_out(self):
        comparison = self._compare("5.00")
        self.assertEqual(comparison["residual_deductible"], "-1.74")
        self.assertTrue(comparison["is_anomaly"])

    def test_default_call_is_byte_for_byte_the_old_behaviour(self):
        plain = compare_gross_to_reported(
            gross_payable=Decimal("11.84"),
            gross_deductible=Decimal("97.02"),
            reported_payable=Decimal("0.00"),
            reported_deductible=Decimal("81.92"),
        )
        self.assertEqual(plain["residual_deductible"], plain["unexplained_deductible"])
        self.assertEqual(plain["explained_adjustments"], [])
        self.assertTrue(plain["is_anomaly"])


class PreflightRefusalTests(unittest.TestCase):
    """The write guard refuses on evidence, never on reconstructed amounts."""

    def setUp(self):
        self.accounts = accounts_by_role()
        self.movements = {
            PAYABLE: _movement(PAYABLE, credit="5232.05"),
            RECEIVABLE: _movement(RECEIVABLE, debit="808.00"),
            SETTLEMENT: _movement(SETTLEMENT),
            ROUNDING: _movement(ROUNDING),
        }

    def _preflight(self, **kwargs):
        return settlement_preflight(
            movements=self.movements,
            accounts=self.accounts,
            existing_journals=kwargs.pop("existing_journals", []),
            reference="BTW-2026-Q2",
            period=Q2,
            journal_date="2026-06-30",
            period_end="2026-06-30",
            **kwargs,
        )

    def test_clean_period_is_clear_to_prepare(self):
        preflight = self._preflight()
        self.assertTrue(preflight["clear_to_prepare"])
        self.assertEqual(preflight["ledger_settlement_occurrences"], [])
        self.assertTrue(preflight["settlement_evidence_complete"])

    def test_a_ledger_occurrence_blocks_the_settlement(self):
        preflight = self._preflight(
            ledger_settlement_occurrences=[
                {
                    "document_type": "VatDocument",
                    "document_id": "vat-doc-q2",
                    "date": "2026-06-30",
                    "amounts_reconstructed": True,
                }
            ]
        )
        self.assertFalse(preflight["clear_to_prepare"])
        self.assertIn("already been cleared", " ".join(preflight["blocking_findings"]))
        self.assertIn("VatDocument", " ".join(preflight["blocking_findings"]))

    def test_the_block_does_not_need_reconstructed_amounts(self):
        preflight = self._preflight(
            ledger_settlement_occurrences=[
                {
                    "document_type": "VatDocument",
                    "document_id": "vat-doc-q2",
                    "date": "2026-06-30",
                    "amounts_reconstructed": False,
                    "payable_restore": None,
                    "receivable_restore": None,
                }
            ]
        )
        self.assertFalse(preflight["clear_to_prepare"])
        self.assertIn(
            "does not depend on whether the exact cleared amounts",
            " ".join(preflight["blocking_findings"]),
        )

    def test_incomplete_evidence_blocks_rather_than_assuming_a_clean_period(self):
        preflight = self._preflight(
            settlement_evidence_complete=False,
            settlement_evidence_gaps=["the receivable account returned no rows"],
        )
        self.assertFalse(preflight["clear_to_prepare"])
        joined = " ".join(preflight["blocking_findings"])
        self.assertIn("could not be established", joined)
        self.assertIn("the receivable account returned no rows", joined)

    def test_a_general_journal_settlement_still_blocks_on_its_own(self):
        preflight = self._preflight(
            existing_journals=[
                {
                    "id": "journal-existing",
                    "reference": "anything",
                    "date": "2026-06-30",
                    "general_journal_document_entries": [
                        {"ledger_account_id": PAYABLE, "debit": "5232.05", "credit": "0.00"},
                        {"ledger_account_id": RECEIVABLE, "debit": "0.00", "credit": "808.00"},
                    ],
                }
            ]
        )
        self.assertFalse(preflight["clear_to_prepare"])
        self.assertIn(
            "settlement-like general journal", " ".join(preflight["blocking_findings"])
        )
