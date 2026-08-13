"""Deterministic bank-mutation matching.

These pin the rules a caller is told fired. Amounts are synthetic; this repo is
public.
"""
from __future__ import annotations

import unittest

from moneybird_mcp.bank_matching import (
    CONFIDENCE_EXACT,
    CONFIDENCE_POSSIBLE,
    CONFIDENCE_STRONG,
    match_mutation,
    match_mutation_groups,
    normalize_iban,
    open_amount,
    score_candidate,
)


def mutation(**overrides):
    base = {
        "id": "9001",
        "date": "2026-06-10",
        "amount": "121.00",
        "amount_open": "121.00",
        "contra_account_name": "Acme Holding BV",
        "contra_account_number": "NL12RABO0123456789",
        "message": "",
        "sepa_fields": {},
    }
    base.update(overrides)
    return base


def sales_invoice(**overrides):
    base = {
        "id": "5001",
        "invoice_id": "2026-014",
        "reference": "",
        "invoice_date": "2026-06-01",
        "total_price_incl_tax": "121.00",
        "total_unpaid": "121.00",
        "contact": {
            "id": "77",
            "company_name": "Acme Holding BV",
            "sepa_iban": "NL12 RABO 0123 4567 89",
        },
    }
    base.update(overrides)
    return base


class ScoreCandidateTests(unittest.TestCase):
    def test_reference_plus_exact_amount_is_exact(self):
        scored = score_candidate(
            mutation(message="Betaling factuur 2026-014"),
            sales_invoice(),
            booking_type="SalesInvoice",
        )
        self.assertEqual(scored["confidence"], CONFIDENCE_EXACT)
        self.assertTrue(scored["amount_matches_exactly"])
        self.assertEqual(scored["booking_id"], "5001")

    def test_reference_survives_stripped_separators(self):
        scored = score_candidate(
            mutation(message="FACTUUR 2026014 ACME"),
            sales_invoice(),
            booking_type="SalesInvoice",
        )
        self.assertEqual(scored["confidence"], CONFIDENCE_EXACT)

    def test_reference_is_read_from_sepa_remi(self):
        scored = score_candidate(
            mutation(sepa_fields={"remi": "Onze ref 2026-014"}),
            sales_invoice(),
            booking_type="SalesInvoice",
        )
        self.assertEqual(scored["confidence"], CONFIDENCE_EXACT)

    def test_short_reference_does_not_match_by_accident(self):
        # A 2-character invoice number would otherwise hit almost any statement.
        scored = score_candidate(
            mutation(message="SEPA overboeking 12345678 spoed", amount="9.99",
                     amount_open="9.99"),
            sales_invoice(invoice_id="12", total_price_incl_tax="9.99",
                          total_unpaid="9.99", contact={"id": "77"}),
            booking_type="SalesInvoice",
        )
        # Amount still matches, but no reference evidence was claimed.
        self.assertNotIn(
            "reference",
            " ".join(scored["evidence"]),
        )

    def test_exact_amount_with_iban_is_strong_without_reference(self):
        scored = score_candidate(
            mutation(), sales_invoice(), booking_type="SalesInvoice"
        )
        self.assertEqual(scored["confidence"], CONFIDENCE_STRONG)
        self.assertTrue(
            any("IBAN" in reason for reason in scored["evidence"]),
            scored["evidence"],
        )

    def test_amount_only_is_possible(self):
        scored = score_candidate(
            mutation(contra_account_name="Onbekend", contra_account_number=""),
            sales_invoice(contact={"id": "77", "company_name": "Andere BV"}),
            booking_type="SalesInvoice",
        )
        self.assertEqual(scored["confidence"], CONFIDENCE_POSSIBLE)

    def test_no_signal_returns_no_candidate(self):
        scored = score_candidate(
            mutation(contra_account_name="Onbekend", contra_account_number=""),
            sales_invoice(
                total_price_incl_tax="88.00",
                total_unpaid="88.00",
                contact={"id": "77", "company_name": "Andere BV"},
            ),
            booking_type="SalesInvoice",
        )
        self.assertIsNone(scored)

    def test_partially_paid_invoice_matches_on_remaining_balance(self):
        scored = score_candidate(
            mutation(amount="21.00", amount_open="21.00"),
            sales_invoice(total_unpaid="21.00"),
            booking_type="SalesInvoice",
        )
        self.assertTrue(scored["amount_matches_exactly"])
        self.assertEqual(scored["open_amount"], "21.00")

    def test_open_amount_falls_back_to_total_minus_payments(self):
        record = sales_invoice(total_unpaid=None)
        record["payments"] = [{"price": "100.00"}]
        self.assertEqual(open_amount(record), open_amount({"total_unpaid": "21.00"}))

    def test_far_past_invoice_is_downgraded_not_dropped(self):
        scored = score_candidate(
            mutation(message="factuur 2026-014"),
            sales_invoice(invoice_date="2019-01-01"),
            booking_type="SalesInvoice",
        )
        self.assertEqual(scored["confidence"], CONFIDENCE_STRONG)
        self.assertTrue(
            any("far from the payment" in reason for reason in scored["evidence"])
        )

    def test_normalize_iban_ignores_spacing_and_case(self):
        self.assertEqual(
            normalize_iban("nl12 rabo 0123 4567 89"), "NL12RABO0123456789"
        )


class MatchMutationTests(unittest.TestCase):
    def test_incoming_money_never_matches_a_purchase_document(self):
        result = match_mutation(
            mutation(),
            sales_invoices=[],
            purchase_documents=[("purchase_invoice", sales_invoice())],
        )
        self.assertEqual(result["direction"], "incoming")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["suggestion"], "none")

    def test_outgoing_money_matches_purchase_documents(self):
        result = match_mutation(
            mutation(amount="-121.00", amount_open="-121.00"),
            sales_invoices=[sales_invoice()],
            purchase_documents=[
                (
                    "purchase_invoice",
                    {
                        "id": "6001",
                        "reference": "F-2026-9",
                        "date": "2026-06-02",
                        "total_price_incl_tax": "121.00",
                        "total_unpaid": "121.00",
                        "contact": {
                            "id": "77",
                            "company_name": "Acme Holding BV",
                            "sepa_iban": "NL12RABO0123456789",
                        },
                    },
                )
            ],
        )
        self.assertEqual(result["direction"], "outgoing")
        self.assertEqual(result["candidates"][0]["booking_type"], "Document")
        self.assertEqual(result["candidates"][0]["document_kind"], "purchase_invoice")

    def test_two_equal_candidates_are_reported_as_ambiguous(self):
        twin = sales_invoice(
            id="5002",
            invoice_id="2026-015",
            contact={
                "id": "77",
                "company_name": "Acme Holding BV",
                "sepa_iban": "NL12RABO0123456789",
            },
        )
        result = match_mutation(
            mutation(),
            sales_invoices=[sales_invoice(), twin],
            purchase_documents=[],
        )
        self.assertEqual(result["suggestion"], "ambiguous")
        self.assertIn("Ask which one", result["note"])

    def test_reference_breaks_an_equal_amount_tie(self):
        twin = sales_invoice(
            id="5002",
            invoice_id="2026-015",
            contact={
                "id": "77",
                "company_name": "Acme Holding BV",
                "sepa_iban": "NL12RABO0123456789",
            },
        )
        result = match_mutation(
            mutation(message="voldoening 2026-015"),
            sales_invoices=[sales_invoice(), twin],
            purchase_documents=[],
        )
        self.assertEqual(result["suggestion"], CONFIDENCE_EXACT)
        self.assertEqual(result["candidates"][0]["booking_id"], "5002")

    def test_unmatched_mutation_points_at_a_ledger_booking(self):
        result = match_mutation(
            mutation(
                amount="-45.00",
                amount_open="-45.00",
                contra_account_name="Shell Nederland",
                contra_account_number="NL99INGB0000000000",
            ),
            sales_invoices=[],
            purchase_documents=[],
        )
        self.assertEqual(result["suggestion"], "none")
        self.assertIn("LedgerAccount", result["note"])

    def test_candidate_list_is_capped(self):
        invoices = [
            sales_invoice(id=str(6000 + index), invoice_id=f"2026-{index:03d}")
            for index in range(10)
        ]
        result = match_mutation(
            mutation(), sales_invoices=invoices, purchase_documents=[],
            max_candidates=3,
        )
        self.assertEqual(len(result["candidates"]), 3)


class MatchMutationGroupTests(unittest.TestCase):
    @staticmethod
    def marktplaats_mutation(item_id: str, amount: str) -> dict:
        return mutation(
            id=item_id,
            date="2026-07-28",
            amount=amount,
            amount_open=amount,
            contra_account_name="Marktplaats B.V.",
            contra_account_number="",
        )

    @staticmethod
    def marktplaats_invoice(item_id: str = "invoice-aug") -> dict:
        return {
            "id": item_id,
            "reference": "MPDI260816662",
            "date": "2026-08-12",
            "state": "new",
            "total_price_incl_tax": "43.19",
            "payments": [],
            "contact": {"company_name": "Marktplaats B.V."},
        }

    def test_unique_exact_group_is_suggested_even_when_invoice_is_later(self):
        mutations = [
            self.marktplaats_mutation("m1", "-11.50"),
            self.marktplaats_mutation("m2", "-13.50"),
            self.marktplaats_mutation("m3", "-18.19"),
        ]
        groups = match_mutation_groups(
            mutations,
            [("purchase_invoice", self.marktplaats_invoice())],
        )

        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0]["suggestion"], "strong")
        self.assertEqual(groups[0]["open_amount"], "43.19")
        self.assertEqual(groups[0]["financial_mutation_ids"], ["m1", "m2", "m3"])
        self.assertTrue(groups[0]["process_purchase_invoice"])

    def test_ambiguous_subsets_and_competing_invoices_stay_ambiguous(self):
        mutations = [
            self.marktplaats_mutation(f"m{index}", amount)
            for index, amount in enumerate(
                ("-11.50", "-31.69", "-13.50", "-29.69"), 1
            )
        ]
        groups = match_mutation_groups(
            mutations, [("purchase_invoice", self.marktplaats_invoice())]
        )
        self.assertEqual(groups[0]["suggestion"], "ambiguous")
        self.assertIn("alternative_financial_mutation_ids", groups[0])

        competing = match_mutation_groups(
            mutations[:2],
            [
                ("purchase_invoice", self.marktplaats_invoice("invoice-a")),
                ("purchase_invoice", self.marktplaats_invoice("invoice-b")),
            ],
        )
        self.assertEqual(len(competing), 2)
        self.assertTrue(all(item["suggestion"] == "ambiguous" for item in competing))

    def test_unsafe_inputs_are_not_grouped(self):
        base = [
            self.marktplaats_mutation("m1", "-11.50"),
            self.marktplaats_mutation("m2", "-31.69"),
        ]
        wrong_contact = [dict(item, contra_account_name="Other B.V.") for item in base]
        processed = [base[0], dict(base[1], state="processed")]
        late_invoice = self.marktplaats_invoice()
        late_invoice["date"] = "2026-10-01"
        for name, mutations, invoice in (
            ("contact", wrong_contact, self.marktplaats_invoice()),
            ("state", processed, self.marktplaats_invoice()),
            ("date", base, late_invoice),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    match_mutation_groups(
                        mutations, [("purchase_invoice", invoice)]
                    ),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
