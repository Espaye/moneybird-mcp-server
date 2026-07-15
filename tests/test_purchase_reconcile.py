import os
import tempfile
import unittest
from decimal import Decimal

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_reconcile_"),
)

from moneybird.purchase_reconcile import (
    build_reconcile_purchase_invoice,
    dutch_month_label,
    scan_purchase_invoices_for_attention,
    _map_lines,
)

LEDGER_ZAK = "470383517976495829"   # Gas, water en elektriciteit (P&L)
TAX_21 = "463484441733366960"       # 21% btw
LEDGER_PRIV = "470713678791967922"  # Onttrekkingen (private)
TAX_GEEN = "470712958464296361"     # Geen btw


def _line(detail_id, description, price, ledger, tax):
    return {
        "id": detail_id,
        "description": description,
        "price": str(price),
        "amount": "1",
        "ledger_account_id": ledger,
        "tax_rate_id": tax,
    }


def _reference_june(doc_id="ref"):
    return {
        "id": doc_id,
        "date": "2026-06-19",
        "state": "paid",
        "reference": "1163421300",
        "prices_are_incl_tax": True,
        "total_price_incl_tax": "825.0",
        "contact": {"id": "C1", "company_name": "Eneco Services B.V."},
        "details": [
            _line("r1", "Eneco termijnnota juni 2026 – stroom zakelijk (60%)", "296.54", LEDGER_ZAK, TAX_21),
            _line("r2", "Eneco termijnnota juni 2026 – gas zakelijk (25%)", "82.70", LEDGER_ZAK, TAX_21),
            _line("r3", "Eneco termijnnota juni 2026 – stroom privé (40%)", "197.68", LEDGER_PRIV, TAX_GEEN),
            _line("r4", "Eneco termijnnota juni 2026 – gas privé (75%)", "248.08", LEDGER_PRIV, TAX_GEEN),
        ],
    }


def _target_july(doc_id="tgt", total="825.0"):
    return {
        "id": doc_id,
        "date": "2026-07-19",
        "state": "new",
        "reference": "1168011272",
        "prices_are_incl_tax": False,
        "total_price_incl_tax": total,
        "contact": {"id": "C1", "company_name": "Eneco Services B.V."},
        "details": [
            _line("L1", "Eneco termijnnota juli 2026 – gas en stroom", "681.82", LEDGER_ZAK, TAX_21),
            _line("L2", "Eneco termijnnota juli 2026 – stroom privé (40%)", "0.0", LEDGER_PRIV, TAX_GEEN),
        ],
    }


class FakeClient:
    def __init__(self, documents):
        # documents: {id: doc}
        self.administration_id = "ADMIN"
        self._docs = {str(d["id"]): d for d in documents}

    def get_document(self, kind, document_id):
        return self._docs[str(document_id)]

    def list_documents(self, kind, *, limit=100, page=1, filter="", period=""):
        return list(self._docs.values())


def _sum_prices(ops):
    return sum(
        (Decimal(op["price"]) for op in ops if "_destroy" not in op),
        Decimal("0"),
    )


class DutchMonthLabelTests(unittest.TestCase):
    def test_parses_iso_date(self):
        self.assertEqual(dutch_month_label("2026-07-19"), "juli 2026")
        self.assertEqual(dutch_month_label("2026-01-01"), "januari 2026")

    def test_bad_input_returns_empty(self):
        self.assertEqual(dutch_month_label(""), "")
        self.assertEqual(dutch_month_label("not-a-date"), "")
        self.assertEqual(dutch_month_label("2026-13-01"), "")


class MapLinesTests(unittest.TestCase):
    def test_reuse_add_and_destroy(self):
        current = [
            {"id": "L1", "ledger_account_id": LEDGER_ZAK, "tax_rate_id": TAX_21},
            {"id": "L2", "ledger_account_id": LEDGER_PRIV, "tax_rate_id": TAX_GEEN},
            {"id": "L3", "ledger_account_id": "OTHER", "tax_rate_id": TAX_21},
        ]
        desired = [
            {"description": "a", "ledger_account_id": LEDGER_ZAK, "tax_rate_id": TAX_21, "price": Decimal("10.00")},
            {"description": "b", "ledger_account_id": LEDGER_PRIV, "tax_rate_id": TAX_GEEN, "price": Decimal("5.00")},
        ]
        ops = _map_lines(current, desired)
        reused = {op["id"] for op in ops if "id" in op and "_destroy" not in op}
        destroyed = {op["id"] for op in ops if op.get("_destroy") == "true"}
        added = [op for op in ops if "id" not in op]
        self.assertEqual(reused, {"L1", "L2"})
        self.assertEqual(destroyed, {"L3"})
        self.assertEqual(added, [])


class BuildReconcileTests(unittest.TestCase):
    def test_equal_totals_verbatim_split_preserves_total(self):
        client = FakeClient([_reference_june(), _target_july()])
        built = build_reconcile_purchase_invoice(
            client, document_id="tgt", reference_document_id="ref"
        )
        payload = built["payload"]
        preview = built["preview"]

        self.assertTrue(payload["prices_are_incl_tax"])
        self.assertEqual(payload["expected_total_incl_tax"], "825.00")

        ops = payload["details_attributes"]
        self.assertEqual(len(ops), 4)
        reused = {op["id"] for op in ops if "id" in op}
        self.assertEqual(reused, {"L1", "L2"})  # both existing lines reused
        self.assertEqual(sum(1 for op in ops if "id" not in op), 2)  # two added
        self.assertFalse(any(op.get("_destroy") for op in ops))
        self.assertEqual(_sum_prices(ops), Decimal("825.00"))

        self.assertEqual(preview["total_before"], "825.00")
        self.assertEqual(preview["total_after"], "825.00")
        self.assertTrue(preview["total_unchanged"])
        self.assertFalse(preview["scaled"])
        self.assertFalse(preview["already_consistent"])

    def test_relabels_month_in_descriptions(self):
        client = FakeClient([_reference_june(), _target_july()])
        built = build_reconcile_purchase_invoice(
            client, document_id="tgt", reference_document_id="ref"
        )
        descriptions = [line["description"] for line in built["preview"]["after_lines"]]
        self.assertTrue(all("juli 2026" in d for d in descriptions))
        self.assertFalse(any("juni 2026" in d for d in descriptions))

    def test_scaling_rebalances_to_exact_total(self):
        client = FakeClient([_reference_june(), _target_july()])
        built = build_reconcile_purchase_invoice(
            client, document_id="tgt", reference_document_id="ref", target_total="800.00"
        )
        payload = built["payload"]
        self.assertEqual(payload["expected_total_incl_tax"], "800.00")
        self.assertEqual(_sum_prices(payload["details_attributes"]), Decimal("800.00"))
        self.assertTrue(built["preview"]["scaled"])
        self.assertTrue(
            any("scaled proportionally" in w for w in built["preview"]["warnings"])
        )

    def test_flips_prices_incl_tax_flag_and_warns(self):
        client = FakeClient([_reference_june(), _target_july()])
        built = build_reconcile_purchase_invoice(
            client, document_id="tgt", reference_document_id="ref"
        )
        self.assertTrue(built["payload"]["prices_are_incl_tax"])
        self.assertFalse(built["preview"]["prices_are_incl_tax_before"])
        self.assertTrue(
            any("prices_are_incl_tax will change" in w for w in built["preview"]["warnings"])
        )

    def test_already_consistent_is_flagged(self):
        # Target already equals the reference structure (same month label reused).
        reference = _reference_june("ref")
        target = _reference_june("tgt")
        target["date"] = "2026-06-19"  # same label so descriptions match verbatim
        client = FakeClient([reference, target])
        built = build_reconcile_purchase_invoice(
            client, document_id="tgt", reference_document_id="ref"
        )
        self.assertTrue(built["preview"]["already_consistent"])
        self.assertTrue(
            any("already matches" in w for w in built["preview"]["warnings"])
        )

    def test_auto_picks_reference_when_omitted(self):
        # Two prior invoices; the 4-line one must win over a 1-line stub.
        stub = {
            "id": "stub",
            "date": "2026-06-25",
            "prices_are_incl_tax": True,
            "total_price_incl_tax": "825.0",
            "contact": {"id": "C1", "company_name": "Eneco Services B.V."},
            "details": [_line("s1", "one liner", "825.0", LEDGER_ZAK, TAX_21)],
        }
        client = FakeClient([_reference_june("ref"), stub, _target_july()])
        built = build_reconcile_purchase_invoice(client, document_id="tgt")
        self.assertEqual(built["preview"]["reference_document_id"], "ref")
        self.assertEqual(len(built["preview"]["after_lines"]), 4)


class ScanAttentionTests(unittest.TestCase):
    def _history(self):
        docs = []
        for idx, month in enumerate(("03", "04", "05")):
            doc = _reference_june(f"good{idx}")
            doc["date"] = f"2026-{month}-19"
            docs.append(doc)
        docs.append(_target_july("bad"))  # 2 lines, incl False, state new
        return docs

    def test_flags_the_deviating_new_invoice(self):
        client = FakeClient(self._history())
        result = scan_purchase_invoices_for_attention(client, period="202601..202612")
        self.assertEqual(result["count"], 1)
        flagged = result["flagged"][0]
        self.assertEqual(flagged["document_id"], "bad")
        reasons = " ".join(flagged["reasons"])
        self.assertIn("new", reasons)
        self.assertIn("usually has 4", reasons)
        self.assertIn("prices_are_incl_tax", reasons)
        self.assertIn(flagged["suggested_reference_document_id"], {"good0", "good1", "good2"})

    def test_healthy_history_flags_nothing(self):
        docs = []
        for idx, month in enumerate(("03", "04", "05", "06")):
            doc = _reference_june(f"ok{idx}")
            doc["date"] = f"2026-{month}-19"
            docs.append(doc)
        client = FakeClient(docs)
        result = scan_purchase_invoices_for_attention(client, period="202601..202612")
        self.assertEqual(result["count"], 0)


if __name__ == "__main__":
    unittest.main()
