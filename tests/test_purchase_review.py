import unittest

from purchase_test_support import (
    LEDGER_PRIV,
    LEDGER_ZAK,
    TAX_GEEN,
    FakeClient,
    line,
    reference_june,
    target_july,
)

from moneybird_mcp.purchase_review import scan_purchase_invoices_for_attention


class ScanAttentionTests(unittest.TestCase):
    def test_flags_the_deviating_new_invoice(self):
        documents = []
        for index, month in enumerate(("03", "04", "05")):
            document = reference_june(f"good{index}")
            document["date"] = f"2026-{month}-19"
            documents.append(document)
        documents.append(target_july("bad"))

        result = scan_purchase_invoices_for_attention(
            FakeClient(documents), period="202601..202612"
        )

        self.assertEqual(result["count"], 1)
        flagged = result["flagged"][0]
        reasons = " ".join(flagged["reasons"])
        self.assertEqual(flagged["document_id"], "bad")
        self.assertIn("new", reasons)
        self.assertIn("usually has 4", reasons)
        self.assertIn("prices_are_incl_tax", reasons)
        self.assertIn(flagged["suggested_reference_document_id"], {"good0", "good1", "good2"})

    def test_healthy_history_flags_nothing(self):
        documents = []
        for index, month in enumerate(("03", "04", "05", "06")):
            document = reference_june(f"ok{index}")
            document["date"] = f"2026-{month}-19"
            documents.append(document)

        result = scan_purchase_invoices_for_attention(
            FakeClient(documents), period="202601..202612"
        )

        self.assertEqual(result["count"], 0)

    def test_one_prior_invoice_is_not_enough_to_claim_a_usual_pattern(self):
        older = reference_june("only-prior")
        target = target_july("target")
        target["state"] = "pending_payment"

        result = scan_purchase_invoices_for_attention(
            FakeClient([target, older]),
            include_description_mapping_checks=False,
        )

        self.assertEqual(result["count"], 0)

    def test_missing_usual_ledger_is_rendered_with_number_and_name(self):
        class NamedLedgerClient(FakeClient):
            def list_ledger_accounts(self):
                return [
                    {"id": LEDGER_ZAK, "account_id": "46801.01", "name": "Algemene kosten"},
                    {"id": LEDGER_PRIV, "account_id": "45185", "name": "Huisvestingskosten"},
                ]

        history = [reference_june(f"good-{index}") for index in range(3)]
        target = target_july("missing-ledger")
        target["details"] = [target["details"][0]]

        result = scan_purchase_invoices_for_attention(
            NamedLedgerClient([target, *history]),
            include_description_mapping_checks=False,
        )

        flagged = next(
            item for item in result["flagged"]
            if item["document_id"] == "missing-ledger"
        )
        reasons = " ".join(flagged["reasons"])
        self.assertIn("45185 Huisvestingskosten", reasons)
        self.assertNotIn(LEDGER_PRIV, reasons)

    def test_description_mapping_check_is_advisory_and_optional(self):
        older, target = self._wetterskip_history()

        advisory = scan_purchase_invoices_for_attention(
            FakeClient([target, older]), contact_id="W1"
        )
        deterministic = scan_purchase_invoices_for_attention(
            FakeClient([target, older]),
            contact_id="W1",
            include_description_mapping_checks=False,
        )

        flagged = next(
            item for item in advisory["flagged"] if item["document_id"] == "wetterskip-new"
        )
        reasons = " ".join(flagged["reasons"])
        self.assertIn("resembles historical", reasons)
        self.assertIn(f"historical {LEDGER_ZAK}", reasons)
        self.assertIn(f"historical {LEDGER_PRIV}", reasons)
        self.assertEqual(deterministic["count"], 0)
        self.assertFalse(deterministic["description_mapping_checks_included"])

    def test_contact_history_scan_pages_past_first_global_hundred(self):
        prior_one = reference_june("prior-one")
        prior_one["date"] = "2025-04-19"
        prior_two = reference_june("prior-two")
        prior_two["date"] = "2025-05-19"
        target = target_july("target")
        unrelated = []
        for index in range(100):
            document = reference_june(f"other-{index}")
            document["contact"] = {"id": f"OTHER-{index}", "company_name": "Other"}
            unrelated.append(document)

        result = scan_purchase_invoices_for_attention(
            FakeClient([target, *unrelated, prior_one, prior_two]),
            contact_id="C1",
            limit=100,
        )

        self.assertEqual(result["scanned"], 3)
        self.assertGreaterEqual(result["pages_scanned"], 2)
        self.assertGreater(result["documents_examined"], 100)
        self.assertTrue(any(item["document_id"] == "target" for item in result["flagged"]))

    def test_contact_history_prefers_sync_to_cover_prior_years(self):
        class SyncHistoryClient(FakeClient):
            def list_document_versions(self, kind, *, filter=""):
                return [
                    {"id": document_id, "version": document.get("version", 1)}
                    for document_id, document in self._docs.items()
                ]

            def fetch_documents_by_ids(self, kind, ids):
                return [self._docs[document_id] for document_id in ids]

            def list_documents(self, *args, **kwargs):
                raise AssertionError("contact history should use synchronization")

        prior_one = reference_june("prior-one")
        prior_one["date"] = "2025-04-19"
        prior_two = reference_june("prior-two")
        prior_two["date"] = "2025-05-19"
        result = scan_purchase_invoices_for_attention(
            SyncHistoryClient([target_july("target"), prior_one, prior_two]),
            contact_id="C1",
        )

        self.assertEqual(result["history_source"], "synchronization")
        self.assertEqual(result["scanned"], 3)
        self.assertTrue(any(item["document_id"] == "target" for item in result["flagged"]))

    @staticmethod
    def _wetterskip_history():
        common = {
            "prices_are_incl_tax": True,
            "contact": {"id": "W1", "company_name": "Wetterskip"},
        }
        older = {
            **common,
            "id": "wetterskip-old",
            "date": "2025-04-30",
            "state": "paid",
            "details": [
                line("o1", "Ingezetenen en verontreinigingsheffing (privé)", "312.50", LEDGER_PRIV, TAX_GEEN),
                line("o2", "Watersysteemheffing gebouwd en ongebouwd (zakelijk)", "499.63", LEDGER_ZAK, TAX_GEEN),
            ],
        }
        target = {
            **common,
            "id": "wetterskip-new",
            "date": "2026-04-30",
            "state": "pending_payment",
            "details": [
                line("n1", "Watersysteemheffing gebouwd en ongebouwd", "654.02", LEDGER_PRIV, TAX_GEEN),
                line("n2", "Watersysteemheffing ingezetenen en verontreinigingsheffing woonruimte", "553.58", LEDGER_ZAK, TAX_GEEN),
            ],
        }
        return older, target


if __name__ == "__main__":
    unittest.main()
