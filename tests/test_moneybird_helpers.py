import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

# Isolate all server state (approvals DB, audit logs, sync caches) from the repo:
# data_dir() reads this env var at call time, so setting it before any test runs is enough.
os.environ["MONEYBIRD_MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="moneybird_mcp_test_state_")

import moneybird_mcp_server as server
from moneybird_mcp import safety


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

    def test_financial_mutation_summary_exposes_reconciliation_state(self) -> None:
        from moneybird_mcp.formatting import compact_financial_mutation_summary

        summary = compact_financial_mutation_summary(
            {
                "id": "123",
                "settlement_state": "settled",
                "amount_open": "0.0",
                "financial_account": {"identifier": "NL00BANK0123456789"},
                "payments": [{"id": "p1"}, {"id": "p2"}],
                "ledger_account_bookings": [{"id": "l1"}],
            },
            "admin",
        )
        self.assertEqual(summary["bookings_count"], 3)
        self.assertEqual(summary["amount_open"], "0.0")
        self.assertEqual(summary["settlement_state"], "settled")
        self.assertEqual(summary["financial_account_name"], "NL00BANK0123456789")

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

    def test_compare_merge_snapshots_detects_language_difference(self) -> None:
        base = {
            "contact_id": "123",
            "scheduled_send_on": "2026-04-16",
            "workflow_id": "wf-a",
            "document_style_id": "ds-1",
            "identity_id": "id-1",
            "currency": "EUR",
            "prices_are_incl_tax": False,
            "discount": "",
            "extra_fields": [],
        }
        mismatches = server.compare_merge_snapshots(
            {**base, "language": "nl"},
            {**base, "language": "en"},
        )
        self.assertEqual([item["label"] for item in mismatches], ["language"])

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

    def test_invoice_line_preview_respects_prices_including_tax(self) -> None:
        from moneybird_mcp.invoicing import build_invoice_line_preview

        row = build_invoice_line_preview(
            customer_id="B4",
            description="Incl",
            entered_total=server.Decimal("121.00"),
            tax_percentage=server.Decimal("21"),
            prices_are_incl_tax=True,
            duplicate_hits=[],
        )
        self.assertEqual(row["amount_excl_tax"], "100.00")
        self.assertEqual(row["amount_tax"], "21.00")
        self.assertEqual(row["amount_incl_tax"], "121.00")

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
        # Redirect the per-administration audit log into a temp dir.
        orig_base = safety.AUDIT_LOG_BASENAME
        orig_legacy = safety.LEGACY_AUDIT_LOG_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                safety.AUDIT_LOG_BASENAME = str(Path(tmp) / ".audit")
                safety.LEGACY_AUDIT_LOG_PATH = Path(tmp) / ".audit.jsonl"
                admin = "admin-1"
                fingerprint = "abc123"
                safety.append_audit_log(
                    {
                        "action": "batch_create_sales_invoices",
                        "fingerprint": fingerprint,
                        "result": "success",
                    },
                    administration_id=admin,
                )
                self.assertTrue(
                    safety.audit_log_contains_success(
                        "batch_create_sales_invoices",
                        fingerprint,
                        administration_id=admin,
                    )
                )
                safety.append_audit_log(
                    {
                        "action": "batch_create_sales_invoices",
                        "fingerprint": fingerprint,
                        "result": "failed",
                    },
                    administration_id=admin,
                )
                self.assertTrue(
                    safety.audit_log_contains_success(
                        "batch_create_sales_invoices",
                        fingerprint,
                        administration_id=admin,
                    )
                )
                safety.append_audit_log(
                    {
                        "action": "batch_create_sales_invoices",
                        "fingerprint": fingerprint,
                        "result": "invalidated",
                    },
                    administration_id=admin,
                )
                self.assertFalse(
                    safety.audit_log_contains_success(
                        "batch_create_sales_invoices",
                        fingerprint,
                        administration_id=admin,
                    )
                )
                # A different tenant must not see this administration's audit entry.
                self.assertFalse(
                    safety.audit_log_contains_success(
                        "batch_create_sales_invoices",
                        fingerprint,
                        administration_id="admin-2",
                    )
                )
            finally:
                safety.AUDIT_LOG_BASENAME = orig_base
                safety.LEGACY_AUDIT_LOG_PATH = orig_legacy


class GuidanceTests(unittest.TestCase):
    def test_playbook_file_exists_and_has_key_sections(self) -> None:
        from moneybird_mcp import guidance

        self.assertTrue(guidance.PLAYBOOK_PATH.exists())
        text = guidance.load_playbook()
        self.assertGreater(len(text), 1000)
        self.assertIn("Gouden regels", text)
        self.assertIn("Consistentie-checklist", text)
        self.assertIn("Scenario-recepten", text)

    def test_every_prompt_carries_hard_rails_inline(self) -> None:
        from moneybird_mcp import guidance

        renderers = [
            guidance.prompt_verwerk_achterstand(period="2025"),
            guidance.prompt_categoriseer_heel_jaar(year="2025"),
            guidance.prompt_leg_cijfers_uit(period="2025"),
            guidance.prompt_factureer_meterverbruik(
                period_label="2026-K2",
                invoice_date="2026-07-16",
                schedule_send_on="2026-07-16",
            ),
            guidance.prompt_aan_de_slag(),
            guidance.prompt_koppel_banktransacties(period="202506"),
        ]
        # Writing scenarios must spell out the approval discipline and the
        # never-invent rule; every scenario points at the playbook resource.
        for text in renderers:
            self.assertIn(guidance.PLAYBOOK_URI, text)
        for text in [renderers[0], renderers[1], renderers[3], renderers[4], renderers[5]]:
            self.assertIn("prepare_", text)
            self.assertIn("_from_approval", text)
            self.assertIn("Verzin NOOIT", text)

    def test_register_guidance_registers_prompts_and_resource(self) -> None:
        import asyncio

        from fastmcp import FastMCP

        from moneybird_mcp import guidance

        scratch = FastMCP(name="guidance-test")
        guidance.register_guidance(scratch)

        async def _collect():
            prompts = await scratch.list_prompts()
            resources = await scratch.list_resources()
            return ({p.name for p in prompts}, {str(r.uri) for r in resources})

        prompt_names, resource_uris = asyncio.run(_collect())
        self.assertEqual(
            prompt_names,
            {
                "aan_de_slag",
                "verwerk_achterstand",
                "categoriseer_heel_jaar",
                "leg_cijfers_uit",
                "diagnose_bankmutatie",
                "koppel_banktransacties",
                "factureer_meterverbruik",
            },
        )
        self.assertIn(guidance.PLAYBOOK_URI, resource_uris)


class CredentialsTests(unittest.TestCase):
    def setUp(self) -> None:
        import fastmcp.server.dependencies as dep

        from moneybird_mcp import credentials as cr

        self.cr = cr
        self.dep = dep
        self._orig_headers = dep.get_http_headers
        # default: behave as if there is no active HTTP request
        dep.get_http_headers = lambda include_all=False, include=None: {}
        self._orig_env = {
            key: os.environ.get(key)
            for key in ("MONEYBIRD_ACCESS_TOKEN", "MONEYBIRD_ADMINISTRATION_ID")
        }

    def tearDown(self) -> None:
        self.dep.get_http_headers = self._orig_headers
        for key, value in self._orig_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_request_headers_take_precedence_over_env(self) -> None:
        self.dep.get_http_headers = lambda include_all=False, include=None: {
            "X-Moneybird-Token": "tenant-token",
            "X-Moneybird-Administration-Id": "42",
        }
        os.environ["MONEYBIRD_ACCESS_TOKEN"] = "env-token"
        creds = self.cr.resolve_credentials()
        self.assertEqual(creds.source, "request")
        self.assertEqual(creds.token, "tenant-token")
        self.assertEqual(creds.administration_id, "42")

    def test_environment_is_the_fallback(self) -> None:
        os.environ["MONEYBIRD_ACCESS_TOKEN"] = "env-token"
        os.environ["MONEYBIRD_ADMINISTRATION_ID"] = "7"
        creds = self.cr.resolve_credentials()
        self.assertEqual(creds.source, "environment")
        self.assertEqual(creds.token, "env-token")
        self.assertEqual(creds.administration_id, "7")

    def test_missing_credentials_raise(self) -> None:
        os.environ.pop("MONEYBIRD_ACCESS_TOKEN", None)
        os.environ.pop("MONEYBIRD_ADMINISTRATION_ID", None)
        with self.assertRaises(server.MoneybirdError):
            self.cr.resolve_credentials()


class SyncIndexPathTests(unittest.TestCase):
    def test_path_is_per_administration_and_sanitized(self) -> None:
        from moneybird_mcp import sync

        # No administration: same basename as the legacy single file, inside data_dir().
        self.assertEqual(sync.sync_index_path(None).name, sync.LEGACY_SYNC_INDEX_PATH.name)
        per_admin = sync.sync_index_path("ab/cd")  # path-unsafe chars are sanitized
        self.assertEqual(per_admin.name, ".moneybird_sync_index_ab_cd.json")
        self.assertNotEqual(per_admin.name, sync.LEGACY_SYNC_INDEX_PATH.name)

    def test_round_trip_and_legacy_migration(self) -> None:
        from moneybird_mcp import sync

        orig_base = sync.SYNC_INDEX_BASENAME
        orig_legacy = sync.LEGACY_SYNC_INDEX_PATH
        with tempfile.TemporaryDirectory() as tmp:
            try:
                sync.SYNC_INDEX_BASENAME = str(Path(tmp) / ".idx")
                sync.LEGACY_SYNC_INDEX_PATH = Path(tmp) / ".idx.json"
                admin = "555"

                # round trip to the per-admin file
                sync.save_sync_index({"administration_id": admin}, admin)
                self.assertTrue(sync.sync_index_path(admin).exists())
                self.assertEqual(sync.load_sync_index(admin)["administration_id"], admin)

                # migration: a legacy file for the same admin is read when no per-admin file
                other = "777"
                sync.LEGACY_SYNC_INDEX_PATH.write_text(
                    '{"administration_id": "777"}', encoding="utf-8"
                )
                self.assertFalse(sync.sync_index_path(other).exists())
                self.assertEqual(sync.load_sync_index(other)["administration_id"], other)

                # saving for that admin writes the per-admin file and drops the legacy one
                sync.save_sync_index({"administration_id": other}, other)
                self.assertTrue(sync.sync_index_path(other).exists())
                self.assertFalse(sync.LEGACY_SYNC_INDEX_PATH.exists())
            finally:
                sync.SYNC_INDEX_BASENAME = orig_base
                sync.LEGACY_SYNC_INDEX_PATH = orig_legacy


class ApprovalSafetyTests(unittest.TestCase):
    def tearDown(self) -> None:
        safety.clear_pending_approvals()

    def test_approval_is_bound_to_administration(self) -> None:
        from moneybird_mcp.credentials import set_active_administration_id

        set_active_administration_id("admin-a")
        approval = safety.make_approval("demo", {"value": 1}, "Demo")
        with self.assertRaises(server.MoneybirdError):
            safety.pop_approval(
                approval["approval_id"],
                "demo",
                administration_id="admin-b",
            )
        pending = safety.pop_approval(
            approval["approval_id"],
            "demo",
            administration_id="admin-a",
        )
        self.assertEqual(pending["administration_id"], "admin-a")

    def test_approval_survives_restart_and_pops_once(self) -> None:
        from moneybird_mcp.credentials import set_active_administration_id

        set_active_administration_id("admin-a")
        approval = safety.make_approval("demo", {"value": 2}, "Demo")
        # Durable: the approval lives in SQLite on disk, not in process memory.
        self.assertTrue(safety.approvals_db_path().exists())
        self.assertGreaterEqual(safety.pending_approval_count(), 1)
        pending = safety.pop_approval(
            approval["approval_id"], "demo", administration_id="admin-a"
        )
        self.assertEqual(pending["payload"], {"value": 2})
        # Single-use: a second pop of the same id must fail.
        with self.assertRaises(server.MoneybirdError):
            safety.pop_approval(
                approval["approval_id"], "demo", administration_id="admin-a"
            )

    def test_expired_approval_is_rejected(self) -> None:
        from moneybird_mcp.credentials import set_active_administration_id

        set_active_administration_id("admin-a")
        approval = safety.make_approval("demo", {"value": 3}, "Demo")
        with safety._approvals_connection() as connection:
            connection.execute(
                "UPDATE approvals SET expires_at = ? WHERE approval_id = ?",
                ("2000-01-01T00:00:00+00:00", approval["approval_id"]),
            )
        with self.assertRaises(server.MoneybirdError) as raised:
            safety.pop_approval(
                approval["approval_id"], "demo", administration_id="admin-a"
            )
        self.assertIn("expired", str(raised.exception))


class ClientRetrySafetyTests(unittest.TestCase):
    def test_write_network_error_is_not_retried(self) -> None:
        import moneybird_mcp.client as client_module

        client = client_module.MoneybirdClient("token", "123")
        pooled_client = mock.Mock()
        pooled_client.request.side_effect = client_module.httpx.ConnectError(
            "lost response"
        )
        with mock.patch.object(
            client_module,
            "get_shared_http_client",
            return_value=pooled_client,
        ):
            with self.assertRaises(server.MoneybirdError) as raised:
                client._request("POST", "/123/sales_invoices.json", body={"x": 1})
        self.assertEqual(pooled_client.request.call_count, 1)
        self.assertIn("ambiguous", str(raised.exception))

    def test_bank_booking_link_translates_price_to_price_base(self) -> None:
        import moneybird_mcp.client as client_module

        client = client_module.MoneybirdClient("token", "123")
        with mock.patch.object(
            client,
            "_request",
            return_value={"id": "456"},
        ) as request:
            client.link_financial_mutation_booking(
                "456",
                {
                    "booking_type": "LedgerAccount",
                    "booking_id": "789",
                    "price": "-10.00",
                },
            )

        request.assert_called_once_with(
            "PATCH",
            "/123/financial_mutations/456/link_booking.json",
            body={
                "booking_type": "LedgerAccount",
                "booking_id": "789",
                "price_base": "10.00",
            },
        )

    def test_bank_invoice_link_keeps_price_in_invoice_currency(self) -> None:
        import moneybird_mcp.client as client_module

        client = client_module.MoneybirdClient("token", "123")
        with mock.patch.object(
            client,
            "_request",
            return_value={"id": "456"},
        ) as request:
            client.link_financial_mutation_booking(
                "456",
                {
                    "booking_type": "SalesInvoice",
                    "booking_id": "789",
                    "price": "-10.00",
                },
            )

        request.assert_called_once_with(
            "PATCH",
            "/123/financial_mutations/456/link_booking.json",
            body={
                "booking_type": "SalesInvoice",
                "booking_id": "789",
                "price": "10.00",
            },
        )


class MeterUsageTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def get_contact_by_customer_id(self, customer_id):
            return {
                "id": f"contact-{customer_id}",
                "customer_id": customer_id,
                "company_name": customer_id,
            }

        def get_contact(self, contact_id):
            return {
                "id": contact_id,
                "customer_id": contact_id.replace("contact-", ""),
            }

        def list_sales_invoices(self, **kwargs):
            customer_id = str(kwargs["contact_id"]).replace("contact-", "")
            return [
                {
                    "id": f"invoice-{customer_id}",
                    "invoice_id": "2026-0001",
                    "invoice_date": "2026-04-16",
                    "state": "paid",
                    "contact_id": f"contact-{customer_id}",
                    "contact": {"id": f"contact-{customer_id}", "customer_id": customer_id},
                    "workflow_id": "workflow",
                    "document_style_id": "style",
                    "identity_id": "identity",
                    "language": "nl",
                    "currency": "EUR",
                    "prices_are_incl_tax": False,
                    "details": [
                        {
                            "description": f"Elektra {customer_id} - 10,00 kWh",
                            "price": "0.34",
                            "tax_rate_id": "tax-21",
                            "ledger_account_id": "ledger-electricity",
                        }
                    ],
                }
            ]

        def list_tax_rates(self):
            return [{"id": "tax-21", "percentage": "21.0"}]

    def test_meter_usage_builds_entries_and_skips_low_usage(self) -> None:
        from moneybird_mcp.invoicing import build_meter_usage_entries

        result = build_meter_usage_entries(
            self.FakeClient(),
            rows=[
                {"meter": "B5", "begin_reading": "233,10", "end_reading": "362,99"},
                {"meter": "B8", "begin_reading": "16,39", "end_reading": "17,00"},
            ],
            period_label="2026-K2",
            invoice_date="2026-07-16",
            schedule_send_on="2026-07-16",
            minimum_usage_kwh="10",
        )
        self.assertEqual(len(result["entries"]), 1)
        entry = result["entries"][0]
        self.assertEqual(entry["reference"], "STROOM-2026-K2-B5")
        self.assertEqual(entry["details"][0]["amount"], "129.89")
        self.assertEqual(entry["details"][0]["price"], "0.34")
        self.assertEqual(result["decisions"][1]["action"], "skip")

    def test_meter_usage_prepare_tool_returns_single_approval_preview(self) -> None:
        from moneybird_mcp import tools
        from moneybird_mcp.credentials import set_active_administration_id
        from moneybird_mcp.tools import _context as tool_context

        fake = self.FakeClient()

        def get_fake_client(*args, **kwargs):
            set_active_administration_id(fake.administration_id)
            return fake

        safety.clear_pending_approvals()
        with mock.patch.object(tool_context, "get_client", side_effect=get_fake_client):
            prepared = tools.prepare_meter_usage_sales_invoices(
                rows=[
                    {
                        "meter": "B5",
                        "begin_reading": "233,10",
                        "end_reading": "362,99",
                        "action": "schedule",
                    }
                ],
                period_label="2026-K2",
                invoice_date="2026-07-16",
                schedule_send_on="2026-07-16",
            )
        self.assertEqual(prepared["action"], "batch_create_sales_invoices")
        self.assertEqual(
            prepared["meter_usage_preview"]["decisions"][0]["source_invoice_id"],
            "invoice-B5",
        )
        self.assertIn("53.43", prepared["preview"]["preview_table"])
        self.assertIn("EUR 53.43 incl. VAT", prepared["summary"])
        self.assertEqual(
            prepared["preview"]["totals_by_currency"]["EUR"],
            {
                "total_price_excl_tax": "44.16",
                "total_tax": "9.27",
                "total_price_incl_tax": "53.43",
            },
        )

    def test_batch_preview_rejects_tax_percentage_mismatch(self) -> None:
        from moneybird_mcp.invoicing import build_batch_invoice_payload

        with self.assertRaises(server.MoneybirdError):
            build_batch_invoice_payload(
                self.FakeClient(),
                {
                    "customer_id": "B5",
                    "invoice_date": "2026-07-16",
                    "details": [
                        {
                            "description": "Elektra B5 - 129,89 kWh",
                            "amount": "129.89",
                            "price": "0.34",
                            "tax_rate_id": "tax-21",
                            "tax_percentage": "9",
                            "ledger_account_id": "ledger-electricity",
                        }
                    ],
                },
            )

    def test_batch_prepare_error_names_the_entry_and_reference(self) -> None:
        from moneybird_mcp.tools import sales_batches

        with mock.patch.object(
            sales_batches,
            "build_batch_invoice_payload",
            side_effect=[{}, server.MoneybirdError("HTTP 404: record not found")],
        ):
            with self.assertRaises(server.MoneybirdError) as caught:
                sales_batches._prepare_batch_create_sales_invoices(
                    self.FakeClient(),
                    [{"reference": "good"}, {"reference": "bad"}],
                )

        self.assertIn("entry[1] (reference 'bad')", str(caught.exception))
        self.assertIn("HTTP 404: record not found", str(caught.exception))


class BatchScheduleTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self):
            self.invoice = {
                "id": "invoice-1",
                "contact_id": "contact-1",
                "contact": {"id": "contact-1", "customer_id": "B9"},
                "state": "draft",
                "invoice_date": "2026-07-16",
                "sent_at": None,
                "total_price_excl_tax": "40.90",
                "total_price_incl_tax": "49.49",
                "workflow_id": "wf",
                "document_style_id": "style",
                "identity_id": "identity",
                "language": "nl",
                "currency": "EUR",
                "prices_are_incl_tax": False,
                "details": [{"description": "Elektra B9 - 120,28 kWh"}],
            }

        def fetch_sales_invoices_by_ids(self, ids):
            return [dict(self.invoice)]

        def get_sales_invoice(self, invoice_id):
            return dict(self.invoice)

        def list_sales_invoices(self, **kwargs):
            return []

        def send_sales_invoice(self, sales_invoice_id, payload):
            self.invoice["state"] = "scheduled"
            self.invoice["invoice_date"] = payload["invoice_date"]
            return dict(self.invoice)

    def test_batch_schedule_executes_and_verifies(self) -> None:
        from moneybird_mcp import tools
        from moneybird_mcp.credentials import set_active_administration_id
        from moneybird_mcp.tools import _context as tool_context

        fake = self.FakeClient()

        def get_fake_client(*args, **kwargs):
            set_active_administration_id(fake.administration_id)
            return fake

        safety.clear_pending_approvals()
        with (
            mock.patch.object(tool_context, "get_client", side_effect=get_fake_client),
            mock.patch.object(tool_context, "audit_log_contains_success", return_value=False),
            mock.patch.object(tool_context, "append_audit_log"),
            mock.patch.object(tool_context, "append_failed_audit_log"),
        ):
            prepared = tools.prepare_batch_schedule_sales_invoices(
                [{"sales_invoice_id": "invoice-1", "invoice_date": "2026-07-16"}]
            )
            result = tools.batch_schedule_sales_invoices_from_approval(
                prepared["approval_id"]
            )
        self.assertTrue(result["all_verified"])
        self.assertEqual(result["verification"][0]["state"], "scheduled")


class _ToolPatches:
    """Context helper: route tools.get_client to a fake and silence the audit log."""

    def __init__(self, fake):
        self.fake = fake

    def __enter__(self):
        from moneybird_mcp.credentials import set_active_administration_id
        from moneybird_mcp.tools import _context as tool_context

        def get_fake_client(*args, **kwargs):
            set_active_administration_id(self.fake.administration_id)
            return self.fake

        safety.clear_pending_approvals()
        self._patches = [
            mock.patch.object(tool_context, "get_client", side_effect=get_fake_client),
            mock.patch.object(tool_context, "audit_log_contains_success", return_value=False),
            mock.patch.object(tool_context, "append_audit_log"),
            mock.patch.object(tool_context, "append_failed_audit_log"),
        ]
        for patch in self._patches:
            patch.start()
        return self

    def __exit__(self, *exc_info):
        for patch in self._patches:
            patch.stop()
        return False


class RegisterPaymentTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self):
            self.invoice = {
                "id": "inv-1",
                "invoice_id": "2026-0001",
                "reference": "2026-0001",
                "state": "open",
                "invoice_date": "2026-06-01",
                "contact": {"company_name": "Klant BV"},
                "total_price_incl_tax": "121.00",
                "total_unpaid": "121.00",
                "payments": [],
            }

        def get_sales_invoice(self, invoice_id):
            return dict(self.invoice)

        def register_sales_invoice_payment(self, invoice_id, payment):
            self.invoice["payments"] = [dict(payment)]
            self.invoice["total_unpaid"] = "0.00"
            return dict(self.invoice)

    def test_full_payment_prepares_and_verifies(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_register_payment(
                document_type="sales_invoice",
                document_id="inv-1",
                payment_date="2026-06-15",
                price="121.00",
            )
            self.assertEqual(prepared["preview"]["document"]["open_amount"], "121.00")
            result = tools.register_payment_from_approval(prepared["approval_id"])
        verification = result["verification"]
        self.assertTrue(verification["total_unchanged_to_the_cent"])
        self.assertTrue(verification["payment_visible_on_document"])
        self.assertEqual(verification["open_amount_after"], "0.00")

    def test_generic_executor_dispatches_pending_action(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_register_payment(
                document_type="sales_invoice",
                document_id="inv-1",
                payment_date="2026-06-15",
                price="121.00",
            )
            result = tools.execute_approved_action(prepared["approval_id"])
        self.assertEqual(result["status"], "payment_registered")
        self.assertTrue(
            result["verification"]["payment_visible_on_document"]
        )
        with self.assertRaises(server.MoneybirdError):
            safety.peek_approval(
                prepared["approval_id"],
                administration_id="admin",
            )

    def test_overpayment_warns_in_preview(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_register_payment(
                document_type="sales_invoice",
                document_id="inv-1",
                payment_date="2026-06-15",
                price="150.00",
            )
        self.assertTrue(
            any("higher than the open amount" in w for w in prepared["preview"]["warnings"])
        )

    def test_rejects_unknown_document_type(self) -> None:
        from moneybird_mcp import tools

        with self.assertRaises(server.MoneybirdError):
            tools.prepare_register_payment(
                document_type="estimate",
                document_id="x",
                payment_date="2026-06-15",
                price="10",
            )


class LinkBankMutationTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self):
            self.last_booking = None
            self.mutation = {
                "id": "mut-1",
                "date": "2026-06-20",
                "message": "Factuur 2026-0001",
                "contra_account_name": "Klant BV",
                "state": "unprocessed",
                "amount": "121.00",
                "amount_open": "121.00",
                "version": 1,
                "payments": [],
                "ledger_account_bookings": [],
            }
            self.invoice = {
                "id": "inv-1",
                "invoice_id": "2026-0001",
                "reference": "2026-0001",
                "state": "open",
                "contact": {"company_name": "Klant BV"},
                "total_price_incl_tax": "121.00",
                "total_unpaid": "121.00",
                "payments": [],
            }

        def get_financial_mutation(self, mutation_id):
            return dict(self.mutation)

        def get_sales_invoice(self, invoice_id):
            return dict(self.invoice)

        def link_financial_mutation_booking(self, mutation_id, booking):
            self.last_booking = dict(booking)
            self.mutation["payments"] = [
                {"id": "pay-1", "price": "121.00", "invoice_id": "inv-1", "invoice_type": "SalesInvoice"}
            ]
            self.mutation["state"] = "processed"
            self.mutation["amount_open"] = "0.00"

    def test_link_executes_and_verifies(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_link_bank_mutation_booking(
                financial_mutation_id="mut-1",
                booking_type="SalesInvoice",
                booking_id="inv-1",
            )
            self.assertIn("amount_open: 121.00", prepared["preview"]["price_note"])
            self.assertEqual(prepared["preview"]["booking"]["price"], "121.00")
            self.assertEqual(
                prepared["preview"]["booking_target"]["total_price_incl_tax"], "121.00"
            )
            result = tools.link_bank_mutation_booking_from_approval(
                prepared["approval_id"]
            )
        self.assertTrue(result["verification"]["new_link_visible_on_mutation"])
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(result["verification"]["after"]["state"], "processed")
        self.assertEqual(fake.last_booking["price"], "121.00")

    def test_no_observable_change_is_reported_as_failed(self) -> None:
        from moneybird_mcp import tools

        class NoOpClient(self.FakeClient):
            def link_financial_mutation_booking(self, mutation_id, booking):
                self.last_booking = dict(booking)

        fake = NoOpClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_link_bank_mutation_booking(
                financial_mutation_id="mut-1",
                booking_type="SalesInvoice",
                booking_id="inv-1",
            )
            result = tools.link_bank_mutation_booking_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["verification"]["write_effect_observed"])
        self.assertFalse(result["verification"]["fully_verified"])

    def test_omitted_price_rejects_a_zero_open_amount(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        fake.mutation["amount_open"] = "0.00"
        with _ToolPatches(fake):
            with self.assertRaisesRegex(server.MoneybirdError, "no open amount"):
                tools.prepare_link_bank_mutation_booking(
                    financial_mutation_id="mut-1",
                    booking_type="SalesInvoice",
                    booking_id="inv-1",
                )

    def test_rejects_invalid_booking_type(self) -> None:
        from moneybird_mcp import tools

        with self.assertRaises(server.MoneybirdError):
            tools.prepare_link_bank_mutation_booking(
                financial_mutation_id="mut-1",
                booking_type="Invoice",
                booking_id="inv-1",
            )


class ReclassifyBankMutationBookingsTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self, *, fail_target_link: bool = False):
            self.fail_target_link = fail_target_link
            self.next_booking = 2
            self.calls = {
                "list_ledger_accounts": 0,
                "fetch_financial_mutations_by_ids": 0,
                "get_financial_mutation": 0,
                "unlink": 0,
                "link": 0,
            }
            self.ledgers = [
                {
                    "id": "ledger-vat",
                    "account_id": "16221",
                    "name": "Betaalde en/of ontvangen btw",
                    "account_type": "current_liabilities",
                    "active": True,
                },
                {
                    "id": "ledger-private-tax",
                    "account_id": "5519.01",
                    "name": "Inkomstenbelasting en ZVW",
                    "account_type": "equity",
                    "active": True,
                },
            ]
            self.mutation = {
                "id": "mut-tax",
                "date": "2026-02-11",
                "version": 7,
                "state": "processed",
                "amount": "-842.00",
                "amount_open": "0.00",
                "message": "",
                "contra_account_name": "Belastingdienst",
                "sepa_fields": {"strd_remi": "0000000000000000"},
                "payments": [],
                "ledger_account_bookings": [
                    {
                        "id": "booking-vat",
                        "ledger_account_id": "ledger-vat",
                        "price": "-842.00",
                        "description": None,
                    }
                ],
            }

        def list_ledger_accounts(self):
            self.calls["list_ledger_accounts"] += 1
            return copy.deepcopy(self.ledgers)

        def fetch_financial_mutations_by_ids(self, ids):
            self.calls["fetch_financial_mutations_by_ids"] += 1
            return [copy.deepcopy(self.mutation)] if "mut-tax" in ids else []

        def get_financial_mutation(self, mutation_id):
            self.calls["get_financial_mutation"] += 1
            return copy.deepcopy(self.mutation)

        def unlink_financial_mutation_booking(
            self, mutation_id, *, booking_type, booking_id
        ):
            self.calls["unlink"] += 1
            self.mutation["ledger_account_bookings"] = [
                booking
                for booking in self.mutation["ledger_account_bookings"]
                if booking["id"] != booking_id
            ]
            self.mutation["amount_open"] = self.mutation["amount"]
            self.mutation["state"] = "unprocessed"
            self.mutation["version"] += 1
            return copy.deepcopy(self.mutation)

        def link_financial_mutation_booking(self, mutation_id, booking):
            self.calls["link"] += 1
            if (
                self.fail_target_link
                and booking["booking_id"] == "ledger-private-tax"
            ):
                raise server.MoneybirdError("simulated target link failure")
            booking_id = f"booking-{self.next_booking}"
            self.next_booking += 1
            self.mutation["ledger_account_bookings"].append(
                {
                    "id": booking_id,
                    "ledger_account_id": booking["booking_id"],
                    "price": booking["price"],
                    "description": None,
                }
            )
            self.mutation["amount_open"] = "0.00"
            self.mutation["version"] += 1
            self.mutation["state"] = "processed"
            self.mutation["version"] += 1
            return copy.deepcopy(self.mutation)

    @staticmethod
    def _entry():
        return {
            "financial_mutation_id": "mut-tax",
            "ledger_account_booking_id": "booking-vat",
            "target_ledger_account_id": "ledger-private-tax",
        }

    def test_batch_reclassifies_and_verifies(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_reclassify_bank_mutation_bookings(
                [self._entry()]
            )
            row = prepared["preview"]["rows"][0]
            self.assertIn("16221", row["source_ledger_account"])
            self.assertIn("5519.01", row["target_ledger_account"])
            result = tools.reclassify_bank_mutation_bookings_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["fully_verified"])
        self.assertEqual(result["not_started_count"], 0)
        self.assertTrue(
            result["completed"][0]["verification"][
                "new_target_booking_visible"
            ]
        )
        self.assertEqual(
            fake.mutation["ledger_account_bookings"][0]["ledger_account_id"],
            "ledger-private-tax",
        )
        self.assertEqual(
            fake.calls,
            {
                "list_ledger_accounts": 2,
                "fetch_financial_mutations_by_ids": 3,
                "get_financial_mutation": 0,
                "unlink": 1,
                "link": 1,
            },
        )
        self.assertEqual(
            result["completed"][0]["verification_source"],
            "independent_batch_refetch",
        )

    def test_combined_workflow_uses_single_parent_approval(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_bookkeeping_correction_batch(
                bank_reclassifications=[self._entry()]
            )
            self.assertEqual(prepared["preview"]["action_count"], 1)
            result = tools.execute_approved_action(prepared["approval_id"])

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["fully_verified"])
        self.assertEqual(
            result["completed"][0]["action"],
            "reclassify_bank_mutation_bookings",
        )
        self.assertEqual(safety.pending_approval_count(), 0)

    def test_target_link_failure_restores_source_booking(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient(fail_target_link=True)
        with _ToolPatches(fake):
            prepared = tools.prepare_reclassify_bank_mutation_bookings(
                [self._entry()]
            )
            result = tools.reclassify_bank_mutation_bookings_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertFalse(result["fully_verified"])
        self.assertTrue(result["failures"][0]["restore"]["source_restored"])
        self.assertEqual(
            fake.mutation["ledger_account_bookings"][0]["ledger_account_id"],
            "ledger-vat",
        )

    def test_restore_does_not_confuse_existing_sibling_with_source(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient(fail_target_link=True)
        fake.mutation["ledger_account_bookings"].append(
            {
                "id": "booking-sibling",
                "ledger_account_id": "ledger-vat",
                "price": "-842.00",
                "description": None,
            }
        )
        with _ToolPatches(fake):
            prepared = tools.prepare_reclassify_bank_mutation_bookings(
                [self._entry()]
            )
            result = tools.reclassify_bank_mutation_bookings_from_approval(
                prepared["approval_id"]
            )

        self.assertTrue(result["failures"][0]["restore"]["source_restored"])
        self.assertEqual(fake.calls["link"], 2)
        self.assertEqual(
            sum(
                booking["ledger_account_id"] == "ledger-vat"
                for booking in fake.mutation["ledger_account_bookings"]
            ),
            2,
        )

    def test_batch_aborts_before_writes_when_version_changed(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_reclassify_bank_mutation_bookings(
                [self._entry()]
            )
            fake.mutation["version"] += 1
            with self.assertRaises(server.MoneybirdError):
                tools.reclassify_bank_mutation_bookings_from_approval(
                    prepared["approval_id"]
                )
        self.assertEqual(
            fake.mutation["ledger_account_bookings"][0]["id"],
            "booking-vat",
        )


class UnlinkBankMutationTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self):
            self.mutation = {
                "id": "mut-1",
                "date": "2026-06-20",
                "message": "Huur juni",
                "state": "processed",
                "amount": "-950.00",
                "version": 1,
                "payments": [],
                "ledger_account_bookings": [
                    {"id": "book-1", "ledger_account_id": "la-9", "price": "-950.00", "description": "Huur"}
                ],
            }

        def get_financial_mutation(self, mutation_id):
            return dict(self.mutation)

        def unlink_financial_mutation_booking(self, mutation_id, *, booking_type, booking_id):
            self.mutation["ledger_account_bookings"] = []
            self.mutation["state"] = "unprocessed"
            self.mutation["version"] += 1

    def test_unlink_requires_existing_booking(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            with self.assertRaises(server.MoneybirdError):
                tools.prepare_unlink_bank_mutation_booking(
                    financial_mutation_id="mut-1",
                    booking_type="LedgerAccountBooking",
                    booking_id="missing",
                )

    def test_unlink_executes_and_verifies(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_unlink_bank_mutation_booking(
                financial_mutation_id="mut-1",
                booking_type="LedgerAccountBooking",
                booking_id="book-1",
            )
            self.assertEqual(prepared["preview"]["unlink"]["id"], "book-1")
            result = tools.unlink_bank_mutation_booking_from_approval(
                prepared["approval_id"]
            )
        self.assertTrue(result["verification"]["booking_removed_from_mutation"])


class CreditInvoiceTests(unittest.TestCase):
    class FakeClient:
        administration_id = "admin"

        def __init__(self):
            self.invoice = {
                "id": "inv-1",
                "invoice_id": "2026-0001",
                "reference": "2026-0001",
                "state": "open",
                "invoice_date": "2026-06-01",
                "contact": {"company_name": "Klant BV"},
                "total_price_incl_tax": "100.00",
                "currency": "EUR",
                "contact_id": "contact-1",
                "details": [],
            }
            self.credit_invoice = None

        def get_sales_invoice(self, invoice_id):
            record = (
                self.credit_invoice
                if invoice_id == "inv-2"
                else self.invoice
            )
            return dict(record)

        def duplicate_sales_invoice_to_credit_invoice(self, invoice_id):
            self.credit_invoice = {
                "id": "inv-2",
                "state": "draft",
                "reference": "2026-0001",
                "total_price_incl_tax": "-100.00",
                "currency": "EUR",
                "contact_id": "contact-1",
                "details": [],
            }
            return dict(self.credit_invoice)

    def test_credit_invoice_verifies_negated_total(self) -> None:
        from moneybird_mcp import tools

        fake = self.FakeClient()
        with _ToolPatches(fake):
            prepared = tools.prepare_create_credit_invoice(sales_invoice_id="inv-1")
            result = tools.create_credit_invoice_from_approval(prepared["approval_id"])
        self.assertEqual(result["credit_invoice"]["state"], "draft")
        self.assertTrue(result["verification"]["credit_negates_original"])


class ReportEndpointTests(unittest.TestCase):
    def test_aging_reports_use_period_until(self) -> None:
        import moneybird_mcp.client as client_module

        client = client_module.MoneybirdClient("token", "123")
        with mock.patch.object(client, "_request", return_value={}) as request:
            client.get_report("debtors_aging", period="20260630")
        request.assert_called_once_with(
            "GET",
            "/123/reports/debtors_aging.json",
            query={"period_until": "20260630"},
        )

    def test_pagination_rejected_for_unpaginated_report(self) -> None:
        import moneybird_mcp.client as client_module

        client = client_module.MoneybirdClient("token", "123")
        with self.assertRaises(server.MoneybirdError):
            client.get_report("profit_loss", period="this_year", page=2)


if __name__ == "__main__":
    unittest.main()
