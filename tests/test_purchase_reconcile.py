import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_reconcile_"),
)

from moneybird.purchase_reconcile import (
    build_explicit_purchase_invoice_reconcile,
    build_reconcile_purchase_invoice,
    dutch_month_label,
    _map_lines,
)
from purchase_test_support import (
    FakeClient,
    LEDGER_PRIV,
    LEDGER_ZAK,
    TAX_21,
    TAX_GEEN,
    line as _line,
    reference_june as _reference_june,
    target_july as _target_july,
)


class PurchaseInvoiceReferenceLookupTests(unittest.TestCase):
    def test_uses_server_side_reference_filter_and_exact_match(self):
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient("token", "admin")
        document = {
            "id": "doc-1",
            "reference": "2112179204",
            "details": [],
            "attachments": [],
        }
        with mock.patch.object(
            client,
            "list_documents",
            return_value=[document],
        ) as list_documents:
            found = client.get_document_by_reference(
                "purchase_invoice",
                "2112179204",
            )

        self.assertIs(found, document)
        list_documents.assert_called_once_with(
            "purchase_invoice",
            limit=100,
            page=1,
            filter="reference:2112179204",
        )

    def test_rejects_ambiguous_exact_reference(self):
        from moneybird.client import MoneybirdClient

        client = MoneybirdClient("token", "admin")
        matches = [
            {"id": "doc-1", "reference": "same"},
            {"id": "doc-2", "reference": "same"},
        ]
        with mock.patch.object(client, "list_documents", return_value=matches):
            with self.assertRaisesRegex(Exception, "matches multiple"):
                client.get_document_by_reference("purchase_invoice", "same")


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
        self.assertEqual(payload["expected_total_before"], "825.00")
        self.assertEqual(payload["expected_total_incl_tax"], "825.00")
        self.assertEqual(payload["expected_version"], "20")
        self.assertEqual(len(payload["expected_lines"]), 4)

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
        self.assertEqual(payload["expected_total_before"], "825.00")
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


class BuildExplicitReconcileTests(unittest.TestCase):
    def test_exact_pdf_split_preserves_total_without_reference_scaling(self):
        target = {
            "id": "wetterskip-2026",
            "version": 42,
            "updated_at": "2026-07-22T13:31:48Z",
            "date": "2026-04-30",
            "state": "pending_payment",
            "reference": "2112179204",
            "prices_are_incl_tax": False,
            "total_price_incl_tax": "1207.60",
            "contact": {"id": "W1", "company_name": "Wetterskip"},
            "details": [
                _line("private", "wrong private", "654.02", LEDGER_PRIV, TAX_GEEN),
                _line("business", "wrong business", "553.58", LEDGER_ZAK, TAX_GEEN),
            ],
        }
        client = FakeClient([target])
        built = build_explicit_purchase_invoice_reconcile(
            client,
            document_id="wetterskip-2026",
            desired_lines=[
                {
                    "description": "Ingezetenen en verontreinigingsheffing (privé)",
                    "price": "344.61",
                    "ledger_account_id": LEDGER_PRIV,
                    "tax_rate_id": TAX_GEEN,
                },
                {
                    "description": "Gebouwd en ongebouwd (zakelijk)",
                    "price": "862.99",
                    "ledger_account_id": LEDGER_ZAK,
                    "tax_rate_id": TAX_GEEN,
                },
            ],
            prices_are_incl_tax=True,
            source_note="PDF page 2",
        )

        self.assertEqual(built["preview"]["mode"], "explicit_lines")
        self.assertEqual(built["preview"]["source_note"], "PDF page 2")
        self.assertTrue(built["preview"]["total_unchanged"])
        self.assertEqual(built["payload"]["expected_total_incl_tax"], "1207.60")
        self.assertEqual(built["payload"]["expected_version"], "42")
        self.assertEqual(_sum_prices(built["payload"]["details_attributes"]), Decimal("1207.60"))

    def test_receipt_accepts_purchase_invoice_ledger_account_type(self):
        client = FakeClient([_target_july("receipt-target")])

        built = build_explicit_purchase_invoice_reconcile(
            client,
            document_id="receipt-target",
            document_kind="receipt",
            desired_lines=[
                {
                    "description": "Exact receipt total",
                    "price": "825.00",
                    "ledger_account_id": LEDGER_PRIV,
                    "tax_rate_id": TAX_GEEN,
                }
            ],
            prices_are_incl_tax=True,
        )

        self.assertEqual(built["payload"]["document_kind"], "receipt")

    def test_rejects_explicit_split_that_changes_total(self):
        client = FakeClient([_target_july(total="825.00")])
        with self.assertRaisesRegex(Exception, "would change the invoice total"):
            build_explicit_purchase_invoice_reconcile(
                client,
                document_id="tgt",
                desired_lines=[
                    {
                        "description": "Incomplete PDF split",
                        "price": "800.00",
                        "ledger_account_id": LEDGER_PRIV,
                        "tax_rate_id": TAX_GEEN,
                    }
                ],
                prices_are_incl_tax=True,
            )


class ReconcileExecutionSafetyTests(unittest.TestCase):
    def test_executes_and_verifies_total_lines_tax_mode_and_version(self):
        from moneybird.tools.purchases import _execute_reconcile

        client = FakeClient([_reference_june(), _target_july()])
        payload = build_reconcile_purchase_invoice(
            client,
            document_id="tgt",
            reference_document_id="ref",
        )["payload"]

        result = _execute_reconcile(client, payload)

        self.assertEqual(result["_status"], "completed")
        self.assertTrue(result["verified_total_unchanged"])
        self.assertTrue(result["verified_lines_match"])
        self.assertTrue(result["verified_prices_are_incl_tax"])
        self.assertEqual(result["version_before"], "20")
        self.assertEqual(result["version_after"], 21)

    def test_aborts_before_write_when_document_version_changed(self):
        from moneybird.tools.purchases import _execute_reconcile

        client = FakeClient([_reference_june(), _target_july()])
        payload = build_reconcile_purchase_invoice(
            client,
            document_id="tgt",
            reference_document_id="ref",
        )["payload"]
        client._docs["tgt"]["version"] = 21

        with self.assertRaisesRegex(Exception, "changed after the preview"):
            _execute_reconcile(client, payload)

        self.assertEqual(client.update_calls, 0)

    def test_target_total_change_uses_prewrite_total_for_concurrency_check(self):
        from moneybird.tools.purchases import _execute_reconcile

        client = FakeClient([_reference_june(), _target_july()])
        payload = build_reconcile_purchase_invoice(
            client,
            document_id="tgt",
            reference_document_id="ref",
            target_total="800.00",
        )["payload"]

        result = _execute_reconcile(client, payload)

        self.assertEqual(client.update_calls, 1)
        self.assertEqual(result["total_after"], "800.00")
        self.assertTrue(result["verified_total_unchanged"])

    def test_full_explicit_prepare_approve_verify_flow(self):
        from moneybird import safety, tools
        from moneybird.credentials import set_active_administration_id
        from moneybird.tools import _context as tool_context

        target = {
            "id": "wetterskip-2026",
            "version": 42,
            "updated_at": "2026-07-22T13:31:48Z",
            "date": "2026-04-30",
            "state": "pending_payment",
            "reference": "2112179204",
            "prices_are_incl_tax": False,
            "total_price_incl_tax": "1207.60",
            "contact": {"id": "W1", "company_name": "Wetterskip"},
            "details": [
                _line("private", "wrong private", "654.02", LEDGER_PRIV, TAX_GEEN),
                _line("business", "wrong business", "553.58", LEDGER_ZAK, TAX_GEEN),
            ],
        }
        client = FakeClient([target])

        def get_fake_client(*args, **kwargs):
            set_active_administration_id(client.administration_id)
            return client

        safety.clear_pending_approvals()
        with (
            mock.patch.object(tool_context, "get_client", side_effect=get_fake_client),
            mock.patch.object(tool_context, "audit_log_contains_success", return_value=False),
            mock.patch.object(tool_context, "append_audit_log"),
            mock.patch.object(tool_context, "append_failed_audit_log"),
        ):
            prepared = tools.prepare_reconcile_purchase_invoice(
                document_id="wetterskip-2026",
                desired_lines=[
                    {
                        "description": "Ingezetenen en verontreinigingsheffing (privé)",
                        "price": "344.61",
                        "ledger_account_id": LEDGER_PRIV,
                        "tax_rate_id": TAX_GEEN,
                    },
                    {
                        "description": "Gebouwd en ongebouwd (zakelijk)",
                        "price": "862.99",
                        "ledger_account_id": LEDGER_ZAK,
                        "tax_rate_id": TAX_GEEN,
                    },
                ],
                prices_are_incl_tax=True,
                source_note="PDF page 2",
            )
            self.assertEqual(prepared["preview"]["mode"], "explicit_lines")
            result = tools.reconcile_purchase_invoice_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["verified_total_unchanged"])
        self.assertTrue(result["verified_lines_match"])
        self.assertEqual(result["total_after"], "1207.60")


if __name__ == "__main__":
    unittest.main()
