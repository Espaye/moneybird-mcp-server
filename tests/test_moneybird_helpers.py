import unittest
from pathlib import Path

import moneybird_mcp_server as server
from moneybird import safety


class MoneybirdHelperTests(unittest.TestCase):
    def test_build_filter_string_adds_period_once(self) -> None:
        self.assertEqual(
            server.build_filter_string(filter="state:all", period="20260101..20261231"),
            "state:all,period:20260101..20261231",
        )
        self.assertEqual(
            server.build_filter_string(
                filter="state:all,period:this_year",
                period="20260101..20261231",
            ),
            "state:all,period:this_year",
        )

    def test_normalize_document_kind_aliases(self) -> None:
        self.assertEqual(server.normalize_document_kind("purchase_invoices"), "purchase_invoice")
        self.assertEqual(server.normalize_document_kind("receipts"), "receipt")
        self.assertEqual(
            server.normalize_document_kind("general_journal_documents"),
            "general_journal_document",
        )

    def test_validate_general_journal_entries_balances(self) -> None:
        result = server.validate_general_journal_entries(
            [
                {"ledger_account_id": "1", "debit": "100.00", "credit": "0.00"},
                {"ledger_account_id": "2", "debit": "0.00", "credit": "100.00"},
            ]
        )
        self.assertEqual(result["total_debit"], "100.00")
        self.assertEqual(result["total_credit"], "100.00")

    def test_validate_general_journal_entries_rejects_unbalanced(self) -> None:
        with self.assertRaises(server.MoneybirdError):
            server.validate_general_journal_entries(
                [
                    {"ledger_account_id": "1", "debit": "100.00", "credit": "0.00"},
                    {"ledger_account_id": "2", "debit": "0.00", "credit": "99.00"},
                ]
            )

    def test_ensure_sync_index_shape_adds_new_buckets(self) -> None:
        index = server.ensure_sync_index_shape({"contacts": {"versions": {}, "records": {}}})
        self.assertIn("purchase_invoices", index)
        self.assertIn("general_journal_documents", index)
        self.assertIn("financial_mutations", index)
        self.assertIn("document_filter", index)
        self.assertIn("financial_mutation_filter", index)

    def test_validate_document_ledger_target_rejects_balance_accounts(self) -> None:
        with self.assertRaises(server.MoneybirdError):
            server.validate_document_ledger_target(
                "purchase_invoice",
                {"id": "1", "account_type": "non_current_assets"},
            )

    def test_year_period_for_date(self) -> None:
        self.assertEqual(server.year_period_for_date("2026-04-16"), "20260101..20261231")
        self.assertEqual(server.year_period_for_date("bad-value"), "this_year")

    def test_build_preview_row_tax_math(self) -> None:
        row = server.build_preview_row(
            customer_id="B4",
            description="Elektra B4 - 37,02 kWh",
            amount_excl_tax=server.money_decimal("12.59"),
            tax_percentage=server.Decimal("21"),
            duplicate_hits=[],
        )
        self.assertEqual(row["amount_excl_tax"], "12.59")
        self.assertEqual(row["amount_tax"], "2.64")
        self.assertEqual(row["amount_incl_tax"], "15.23")
        self.assertEqual(row["status"], "ready")

    def test_duplicate_fingerprint_is_stable(self) -> None:
        payload = {"a": 1, "b": ["x", "y"]}
        first = server.duplicate_fingerprint("batch_create_sales_invoices", payload)
        second = server.duplicate_fingerprint("batch_create_sales_invoices", payload)
        self.assertEqual(first, second)

    def test_compare_merge_snapshots_detects_workflow_difference(self) -> None:
        mismatches = server.compare_merge_snapshots(
            {
                "contact_id": "123",
                "scheduled_send_on": "2026-04-16",
                "workflow_id": "wf-a",
                "document_style_id": "ds-1",
                "identity_id": "id-1",
                "currency": "EUR",
                "prices_are_incl_tax": False,
                "discount": "",
                "extra_fields": [],
            },
            {
                "contact_id": "123",
                "scheduled_send_on": "2026-04-16",
                "workflow_id": "wf-b",
                "document_style_id": "ds-1",
                "identity_id": "id-1",
                "currency": "EUR",
                "prices_are_incl_tax": False,
                "discount": "",
                "extra_fields": [],
            },
        )
        self.assertEqual([item["label"] for item in mismatches], ["workflow"])

    def test_evaluate_merge_compatibility_marks_exact_match(self) -> None:
        result = server.evaluate_merge_compatibility(
            {
                "contact_id": "123",
                "customer_id": "B4",
                "scheduled_send_on": "2026-04-16",
                "workflow_id": "wf-a",
                "document_style_id": "ds-1",
                "identity_id": "id-1",
                "currency": "EUR",
                "prices_are_incl_tax": False,
                "discount": "",
                "extra_fields": [],
            },
            [
                {
                    "id": "900",
                    "invoice_id": "2026-001",
                    "contact_id": "123",
                    "invoice_date": "2026-04-16",
                    "workflow_id": "wf-a",
                    "document_style_id": "ds-1",
                    "identity_id": "id-1",
                    "currency": "EUR",
                    "prices_are_incl_tax": False,
                    "contact": {"customer_id": "B4"},
                }
            ],
        )
        self.assertEqual(result["status"], "compatible")
        self.assertEqual(
            result["matching_existing_invoices"][0]["sales_invoice_id"],
            "900",
        )

    def test_apply_batch_group_merge_checks_marks_warning(self) -> None:
        batch_items = [
            {
                "contact": {"id": "123", "customer_id": "B4"},
                "schedule_send_on": "2026-04-16",
                "merge_snapshot": {
                    "contact_id": "123",
                    "scheduled_send_on": "2026-04-16",
                    "workflow_id": "wf-a",
                    "document_style_id": "ds-1",
                    "identity_id": "id-1",
                    "currency": "EUR",
                    "prices_are_incl_tax": False,
                    "discount": "",
                    "extra_fields": [],
                },
                "merge_check": {"status": "no_existing_candidates", "warnings": []},
            },
            {
                "contact": {"id": "123", "customer_id": "B4"},
                "schedule_send_on": "2026-04-16",
                "merge_snapshot": {
                    "contact_id": "123",
                    "scheduled_send_on": "2026-04-16",
                    "workflow_id": "wf-b",
                    "document_style_id": "ds-1",
                    "identity_id": "id-1",
                    "currency": "EUR",
                    "prices_are_incl_tax": False,
                    "discount": "",
                    "extra_fields": [],
                },
                "merge_check": {"status": "no_existing_candidates", "warnings": []},
            },
        ]
        server.apply_batch_group_merge_checks(batch_items)
        self.assertEqual(batch_items[0]["merge_check"]["status"], "warning")
        self.assertIn(
            "workflow",
            batch_items[0]["merge_check"]["batch_group_mismatch_fields"],
        )

    def test_render_preview_table_contains_headers(self) -> None:
        table = server.render_preview_table(
            [
                {
                    "customer_id": "B4",
                    "description": "Elektra B4 - 37,02 kWh",
                    "amount_excl_tax": "12.59",
                    "amount_tax": "2.64",
                    "amount_incl_tax": "15.23",
                    "status": "ready",
                }
            ]
        )
        self.assertIn("customer", table)
        self.assertIn("Elektra B4 - 37,02 kWh", table)

    def test_contact_invoice_email_prefers_invoice_email(self) -> None:
        contact = {
            "email": "general@example.com",
            "send_invoices_to_email": "invoice@example.com",
        }
        self.assertEqual(server.contact_invoice_email(contact), "invoice@example.com")

    def test_recurring_sales_invoice_delivery_issue_detects_manual_contact(self) -> None:
        issue = server.recurring_sales_invoice_delivery_issue(
            {
                "id": "rec-1",
                "active": True,
                "auto_send": True,
                "invoice_date": "2026-05-01",
                "contact": {
                    "id": "contact-1",
                    "delivery_method": "Manual",
                    "email": "customer@example.com",
                },
            },
            "admin-1",
        )
        self.assertIsNotNone(issue)
        self.assertEqual(issue["reasons"], ["contact_delivery_method=Manual"])

    def test_recurring_sales_invoice_delivery_issue_accepts_email_auto_send(self) -> None:
        issue = server.recurring_sales_invoice_delivery_issue(
            {
                "id": "rec-1",
                "active": True,
                "auto_send": True,
                "contact": {
                    "id": "contact-1",
                    "delivery_method": "Email",
                    "send_invoices_to_email": "customer@example.com",
                },
            },
            "admin-1",
        )
        self.assertIsNone(issue)

    def test_classify_sales_invoice_send_detects_manual_and_automatic(self) -> None:
        manual = server.classify_sales_invoice_send(
            {
                "id": "inv-1",
                "events": [
                    {"action": "sales_invoice_send_manually", "created_at": "2026-04-24T03:38:29Z"},
                ],
                "contact": {"id": "contact-1", "delivery_method": "Manual"},
            },
            "admin-1",
        )
        automatic = server.classify_sales_invoice_send(
            {
                "id": "inv-2",
                "recurring_sales_invoice_id": "rec-1",
                "events": [
                    {
                        "action": "sales_invoice_state_changed_to_scheduled",
                        "created_at": "2026-04-24T01:00:00Z",
                    },
                    {"action": "sales_invoice_send_email", "created_at": "2026-04-24T03:38:29Z"},
                ],
                "contact": {"id": "contact-2", "delivery_method": "Email"},
            },
            "admin-1",
        )
        self.assertEqual(manual["classification"], "manual")
        self.assertEqual(automatic["classification"], "automatic_email")

    def test_render_contact_delivery_table_contains_contact(self) -> None:
        table = server.render_contact_delivery_table(
            [
                {
                    "customer_id": "S1",
                    "title": "Contact: Example Customer",
                    "delivery_method": "Manual",
                    "invoice_email": "customer@example.com",
                }
            ]
        )
        self.assertIn("customer", table)
        self.assertIn("Example Customer", table)

    def test_audit_log_contains_success(self) -> None:
        # Patch the audit-log path on the module that actually reads it.
        original_path = safety.AUDIT_LOG_PATH
        test_path = Path("tests") / "tmp_audit_log.jsonl"
        try:
            if test_path.exists():
                test_path.unlink()
            safety.AUDIT_LOG_PATH = test_path
            fingerprint = "abc123"
            safety.append_audit_log(
                {
                    "action": "batch_create_sales_invoices",
                    "fingerprint": fingerprint,
                    "result": "success",
                }
            )
            self.assertTrue(
                safety.audit_log_contains_success(
                    "batch_create_sales_invoices",
                    fingerprint,
                )
            )
        finally:
            safety.AUDIT_LOG_PATH = original_path
            if test_path.exists():
                test_path.unlink()


if __name__ == "__main__":
    unittest.main()
