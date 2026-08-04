from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import sales


class SalesWorkflowWriteSafetyTests(unittest.TestCase):
    class FakeClient:
        administration_id = "sales-admin"

        def __init__(self) -> None:
            self.record = {
                "id": "123",
                "invoice_id": "2026-0001",
                "contact_id": "contact-1",
                "state": "scheduled",
                "paused": False,
                "invoice_date": "2026-08-01",
                "currency": "EUR",
                "total_price_incl_tax": "108.89",
                "version": 1,
                "updated_at": "2026-07-30T10:00:00Z",
            }
            self.pause_calls = 0
            self.resume_calls = 0

        def get_sales_invoice(self, sales_invoice_id: str):
            assert sales_invoice_id == "123"
            return dict(self.record)

        def get_contact(self, contact_id: str):
            assert contact_id == "contact-1"
            return {
                "id": contact_id,
                "delivery_method": "Email",
                "send_invoices_to_email": "klant@example.com",
            }

        def pause_sales_invoice(self, sales_invoice_id: str):
            assert sales_invoice_id == "123"
            self.pause_calls += 1
            self.record["paused"] = True
            self._advance()
            return dict(self.record)

        def resume_sales_invoice(self, sales_invoice_id: str):
            assert sales_invoice_id == "123"
            self.resume_calls += 1
            self.record["paused"] = False
            self._advance()
            return dict(self.record)

        def _advance(self) -> None:
            self.record["version"] += 1
            self.record["updated_at"] = (
                f"2026-07-30T10:0{self.record['version']}:00Z"
            )

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="moneybird_sales_write_"
        )
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id(None)

    def tearDown(self) -> None:
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    def test_pause_resume_pause_uses_state_aware_fingerprints(self) -> None:
        fake = self.FakeClient()

        def resolve_client():
            set_active_administration_id(fake.administration_id)
            return fake

        with mock.patch.object(
            sales.ctx,
            "get_client",
            side_effect=resolve_client,
        ):
            first_pause = sales.prepare_pause_sales_invoice_workflow("123")
            self.assertIn(
                "automatic workflow",
                first_pause["preview"]["effect"],
            )
            first_result = sales.pause_sales_invoice_workflow_from_approval(
                first_pause["approval_id"]
            )
            resume = sales.prepare_resume_sales_invoice_workflow("123")
            resume_result = sales.resume_sales_invoice_workflow_from_approval(
                resume["approval_id"]
            )
            second_pause = sales.prepare_pause_sales_invoice_workflow("123")
            second_result = sales.pause_sales_invoice_workflow_from_approval(
                second_pause["approval_id"]
            )

        self.assertEqual(first_result["status"], "paused")
        self.assertEqual(resume_result["status"], "resumed")
        self.assertEqual(second_result["status"], "paused")
        self.assertNotEqual(
            first_pause["payload"]["fingerprint"],
            second_pause["payload"]["fingerprint"],
        )
        self.assertEqual(fake.pause_calls, 2)
        self.assertEqual(fake.resume_calls, 1)

    def test_draft_invoice_preview_shows_line_subtotals_and_total(self) -> None:
        class InvoiceClient:
            administration_id = "sales-admin"

            def get_contact(self, contact_id: str):
                return {
                    "id": contact_id,
                    "customer_id": "C-1",
                    "company_name": "Preview customer",
                }

            def list_sales_invoices(self, **_kwargs):
                # Latest-invoice defaults reproduce Moneybird's normal 21%
                # excl.-VAT sales setup without contacting the live API.
                return [
                    {
                        "id": "old-invoice",
                        "invoice_date": "2026-07-01",
                        "prices_are_incl_tax": False,
                        "currency": "EUR",
                        "reference": "Verify total",
                        "total_price_excl_tax": "90.00",
                        "total_price_incl_tax": "108.90",
                        "details": [
                            {
                                "description": "Different line",
                                "tax_rate_id": "tax-21",
                                "ledger_account_id": "ledger-sales",
                            }
                        ],
                    }
                ]

            def list_tax_rates(self):
                return [{"id": "tax-21", "percentage": "21"}]

        client = InvoiceClient()
        set_active_administration_id(client.administration_id)
        with mock.patch.object(sales.ctx, "get_client", return_value=client):
            prepared = sales.prepare_create_sales_invoice_draft(
                "contact-1",
                [
                    {
                        "description": "Verification line",
                        "price": "1000.00",
                        "amount": "3",
                    }
                ],
                reference="Verify total",
            )

        preview = prepared["preview"]
        self.assertIn("EUR 3630.00 incl. VAT", prepared["summary"])
        self.assertEqual(preview["total_price_excl_tax"], "3000.00")
        self.assertEqual(preview["total_tax"], "630.00")
        self.assertEqual(preview["total_price_incl_tax"], "3630.00")
        self.assertEqual(preview["line_items"][0]["quantity"], "3")
        self.assertEqual(preview["line_items"][0]["unit_price"], "1000.00")
        self.assertEqual(preview["line_items"][0]["amount_incl_tax"], "3630.00")
        duplicate = preview["potential_duplicates"][0]
        self.assertEqual(duplicate["currency"], "EUR")
        self.assertEqual(duplicate["total_price_excl_tax"], "90.00")
        self.assertEqual(duplicate["total_price_incl_tax"], "108.90")
        self.assertEqual(
            prepared["payload"]["details_attributes"][0]["tax_rate_id"],
            "tax-21",
        )

    def test_first_invoice_uses_product_tax_and_ledger_defaults(self) -> None:
        class FirstInvoiceClient:
            administration_id = "sales-admin"

            def get_contact(self, contact_id: str):
                return {
                    "id": contact_id,
                    "customer_id": "C-NEW",
                    "company_name": "New customer",
                }

            def list_sales_invoices(self, **_kwargs):
                return []

            def get_product(self, product_id: str):
                self.requested_product_id = product_id
                return {
                    "id": product_id,
                    "tax_rate_id": "tax-9",
                    "ledger_account_id": "ledger-product",
                }

            def list_tax_rates(self):
                return [{"id": "tax-9", "percentage": "9"}]

        client = FirstInvoiceClient()
        set_active_administration_id(client.administration_id)
        with mock.patch.object(sales.ctx, "get_client", return_value=client):
            prepared = sales.prepare_create_sales_invoice_draft(
                "contact-new",
                [
                    {
                        "description": "First product invoice",
                        "price": "100.00",
                        "product_id": "product-9",
                    }
                ],
            )

        line = prepared["payload"]["details_attributes"][0]
        self.assertEqual(client.requested_product_id, "product-9")
        self.assertEqual(line["tax_rate_id"], "tax-9")
        self.assertEqual(line["ledger_account_id"], "ledger-product")
        self.assertEqual(line["product_id"], "product-9")
        self.assertEqual(prepared["preview"]["total_price_incl_tax"], "109.00")

    def test_send_preview_summary_resolves_email_recipient_and_total(self) -> None:
        client = self.FakeClient()
        set_active_administration_id(client.administration_id)
        with mock.patch.object(sales.ctx, "get_client", return_value=client):
            prepared = sales.prepare_send_sales_invoice("123")

        self.assertEqual(
            prepared["summary"],
            "Email invoice 2026-0001 (EUR 108.89) to klant@example.com",
        )
        self.assertEqual(
            prepared["payload"]["sales_invoice_sending"]["email_address"],
            "klant@example.com",
        )
        self.assertEqual(prepared["preview"]["delivery_method"], "Email")

    def test_manual_send_summary_says_that_no_email_will_be_sent(self) -> None:
        client = self.FakeClient()
        client.record.update(invoice_id="2026-0002", total_price_incl_tax="42.00")
        set_active_administration_id(client.administration_id)
        with mock.patch.object(sales.ctx, "get_client", return_value=client):
            prepared = sales.prepare_send_sales_invoice(
                "123",
                delivery_method="Manual",
            )

        self.assertEqual(
            prepared["summary"],
            "Mark invoice 2026-0002 (EUR 42.00) as sent (Manual, no email)",
        )
        self.assertNotIn(
            "email_address",
            prepared["payload"]["sales_invoice_sending"],
        )


if __name__ == "__main__":
    unittest.main()
