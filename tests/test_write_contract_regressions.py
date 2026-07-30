from __future__ import annotations

import copy
import os
import tempfile
import unittest
from unittest import mock

from moneybird import safety
from moneybird.config import MoneybirdError
from moneybird.credentials import set_active_administration_id
from moneybird.tools import bank, ledger, payments, sales, sales_batches
from moneybird.tools.approvals import APPROVAL_EXECUTORS
from moneybird.tools._writes import (
    mark_write_dispatch_started,
    run_approved_write,
)
from moneybird.write_contracts import WRITE_SPECS


class WriteContractRegressionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="moneybird_write_contracts_"
        )
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id("contract-admin")

    def tearDown(self) -> None:
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    @staticmethod
    def _patch_client(module, client):
        return mock.patch.object(module.ctx, "get_client", return_value=client)

    def test_every_approval_executor_has_a_complete_versioned_write_spec(self) -> None:
        self.assertEqual(set(APPROVAL_EXECUTORS), set(WRITE_SPECS))
        for action, spec in WRITE_SPECS.items():
            self.assertGreaterEqual(spec.schema_version, 1, action)
            self.assertTrue(spec.precondition.strip(), action)
            self.assertTrue(spec.verifier.strip(), action)
            self.assertTrue(spec.idempotency.strip(), action)
            self.assertTrue(spec.reconciliation.strip(), action)

    def test_preexisting_identical_payment_does_not_verify_a_noop(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.calls = 0
                self.record = {
                    "id": "inv-1",
                    "invoice_id": "2026-001",
                    "state": "open",
                    "contact": {"company_name": "Klant"},
                    "total_price_incl_tax": "100.00",
                    "payments": [
                        {"payment_date": "2026-07-01", "price": "10.00"}
                    ],
                    "version": 1,
                }

            def get_sales_invoice(self, _invoice_id):
                return copy.deepcopy(self.record)

            def register_sales_invoice_payment(self, _invoice_id, _payment):
                self.calls += 1
                # Deliberately do nothing: an identical payment already existed.

        client = Client()
        with self._patch_client(payments, client):
            prepared = payments.prepare_register_payment(
                "sales_invoice",
                "inv-1",
                "2026-07-01",
                "10.00",
            )
            result = payments.register_payment_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(client.calls, 1)
        self.assertFalse(result["verification"]["exact_new_payment_delta"])
        self.assertEqual(
            safety.approval_execution_state(
                prepared["approval_id"],
                administration_id=client.administration_id,
            )["state"],
            "verification_failed",
        )

    def test_payment_with_wrong_controlled_metadata_is_not_verified(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.record = {
                    "id": "inv-1",
                    "invoice_id": "2026-001",
                    "state": "open",
                    "contact": {"company_name": "Klant"},
                    "total_price_incl_tax": "100.00",
                    "payments": [],
                    "version": 1,
                }

            def get_sales_invoice(self, _invoice_id):
                return copy.deepcopy(self.record)

            def register_sales_invoice_payment(self, _invoice_id, payment):
                self.record["payments"].append(
                    {
                        **payment,
                        "financial_account_id": "wrong-account",
                    }
                )
                self.record["version"] += 1

        client = Client()
        with self._patch_client(payments, client):
            prepared = payments.prepare_register_payment(
                "sales_invoice",
                "inv-1",
                "2026-07-01",
                "10.00",
                financial_account_id="expected-account",
            )
            result = payments.register_payment_from_approval(
                prepared["approval_id"]
            )

        self.assertFalse(result["verification"]["exact_new_payment_delta"])
        self.assertEqual(
            safety.approval_execution_state(
                prepared["approval_id"],
                administration_id=client.administration_id,
            )["state"],
            "verification_failed",
        )

    def test_wrong_journal_lines_with_same_count_are_not_verified(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def list_ledger_accounts(self):
                return [
                    {"id": "a", "name": "A"},
                    {"id": "b", "name": "B"},
                ]

            def create_general_journal_document(self, _payload):
                return {"id": "journal-1"}

            def get_document(self, _kind, _document_id):
                return {
                    "id": "journal-1",
                    "reference": "R",
                    "date": "2026-07-01",
                    "description": "D",
                    "details": [
                        {
                            "ledger_account_id": "wrong-a",
                            "debit": "999",
                            "credit": "0",
                        },
                        {
                            "ledger_account_id": "wrong-b",
                            "debit": "0",
                            "credit": "999",
                        },
                    ],
                }

        client = Client()
        with self._patch_client(ledger, client):
            prepared = ledger.prepare_create_general_journal_document(
                "R",
                "2026-07-01",
                [
                    {"ledger_account_id": "a", "debit": "10", "credit": "0"},
                    {"ledger_account_id": "b", "debit": "0", "credit": "10"},
                ],
                "D",
            )
            result = ledger.create_general_journal_document_from_approval(
                prepared["approval_id"]
            )

        self.assertFalse(result["verification"]["fully_verified"])
        self.assertTrue(result["verification"]["line_mismatches"])

    def test_wrong_invoice_line_with_same_count_is_not_verified(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def get_contact(self, _contact_id):
                return {"id": "contact-1"}

            def create_sales_invoice(self, _payload):
                return {"id": "invoice-1"}

            def get_sales_invoice(self, _invoice_id):
                return {
                    "id": "invoice-1",
                    "contact_id": "contact-1",
                    "currency": "EUR",
                    "details": [
                        {
                            "description": "Wrong",
                            "price": "999",
                            "amount": "1",
                            "ledger_account_id": "wrong",
                        }
                    ],
                }

        client = Client()
        with self._patch_client(sales, client):
            prepared = sales.prepare_create_sales_invoice_draft(
                "contact-1",
                [
                    {
                        "description": "Expected",
                        "price": "10",
                        "amount": "1",
                        "ledger_account_id": "ledger-1",
                    }
                ],
            )
            result = sales.create_sales_invoice_draft_from_approval(
                prepared["approval_id"]
            )

        self.assertFalse(result["verification"]["fully_verified"])
        self.assertTrue(result["verification"]["line_mismatches"])

    def test_stale_batch_update_fails_before_any_write(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.calls = 0
                self.record = {
                    "id": "invoice-1",
                    "invoice_id": "2026-001",
                    "version": 1,
                    "contact": {"customer_id": "C1"},
                    "details": [
                        {
                            "id": "line-1",
                            "row_order": 0,
                            "description": "Before",
                        }
                    ],
                }

            def get_sales_invoice(self, _invoice_id):
                return copy.deepcopy(self.record)

            def update_sales_invoice(self, _invoice_id, _patch):
                self.calls += 1
                return copy.deepcopy(self.record)

        client = Client()
        with self._patch_client(sales_batches, client):
            prepared = sales_batches.prepare_batch_update_sales_invoices(
                [
                    {
                        "sales_invoice_id": "invoice-1",
                        "detail_updates": [
                            {"row_order": 0, "description": "After"}
                        ],
                    }
                ]
            )
            client.record["version"] = 2
            with self.assertRaisesRegex(MoneybirdError, "changed after preview"):
                sales_batches.batch_update_sales_invoices_from_approval(
                    prepared["approval_id"]
                )

        self.assertEqual(client.calls, 0)
        execution = safety.approval_execution_state(
            prepared["approval_id"],
            administration_id=client.administration_id,
        )
        self.assertEqual(execution["state"], "failed_pre_write")
        self.assertEqual(execution["phase"], "completed")

    def test_repeatable_unlink_uses_mutation_occurrence(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.record = {
                    "id": "mutation-1",
                    "version": 1,
                    "state": "processed",
                    "amount": "-10.00",
                    "amount_open": "0.00",
                    "payments": [],
                    "ledger_account_bookings": [
                        {
                            "id": "booking-1",
                            "ledger_account_id": "ledger-1",
                            "price": "-10.00",
                        }
                    ],
                }

            def get_financial_mutation(self, _mutation_id):
                return copy.deepcopy(self.record)

            def unlink_financial_mutation_booking(
                self,
                _mutation_id,
                *,
                booking_type,
                booking_id,
            ):
                self.record["ledger_account_bookings"] = []
                self.record["state"] = "unprocessed"
                self.record["amount_open"] = "-10.00"
                self.record["version"] += 1

            def relink_same_booking(self):
                self.record["ledger_account_bookings"] = [
                    {
                        "id": "booking-1",
                        "ledger_account_id": "ledger-1",
                        "price": "-10.00",
                    }
                ]
                self.record["state"] = "processed"
                self.record["amount_open"] = "0.00"
                self.record["version"] += 1

        client = Client()
        with self._patch_client(bank, client):
            first = bank.prepare_unlink_bank_mutation_booking(
                "mutation-1",
                "LedgerAccountBooking",
                "booking-1",
            )
            bank.unlink_bank_mutation_booking_from_approval(first["approval_id"])
            client.relink_same_booking()
            second = bank.prepare_unlink_bank_mutation_booking(
                "mutation-1",
                "LedgerAccountBooking",
                "booking-1",
            )
            bank.unlink_bank_mutation_booking_from_approval(second["approval_id"])

        self.assertNotEqual(
            first["payload"]["fingerprint"],
            second["payload"]["fingerprint"],
        )

    def test_bank_link_does_not_accept_a_different_target_with_same_price(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.record = {
                    "id": "mutation-1",
                    "version": 1,
                    "state": "unprocessed",
                    "amount": "-10.00",
                    "amount_open": "-10.00",
                    "payments": [],
                    "ledger_account_bookings": [],
                }

            def get_financial_mutation(self, _mutation_id):
                return copy.deepcopy(self.record)

            def get_ledger_account(self, ledger_id):
                return {"id": ledger_id, "name": ledger_id}

            def link_financial_mutation_booking(self, _mutation_id, _booking):
                self.record["ledger_account_bookings"] = [
                    {
                        "id": "booking-1",
                        "ledger_account_id": "different-ledger",
                        "price": "-10.00",
                    }
                ]
                self.record["amount_open"] = "0.00"
                self.record["state"] = "processed"
                self.record["version"] += 1

        client = Client()
        with self._patch_client(bank, client):
            prepared = bank.prepare_link_bank_mutation_booking(
                "mutation-1",
                "LedgerAccount",
                "expected-ledger",
                "-10.00",
            )
            result = bank.link_bank_mutation_booking_from_approval(
                prepared["approval_id"]
            )

        self.assertFalse(result["verification"]["booking_target_matches"])
        self.assertFalse(result["verification"]["fully_verified"])
        self.assertEqual(
            safety.approval_execution_state(
                prepared["approval_id"],
                administration_id=client.administration_id,
            )["state"],
            "verification_failed",
        )

    def test_bank_link_target_change_fails_before_dispatch(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.target_version = 1
                self.link_calls = 0
                self.record = {
                    "id": "mutation-1",
                    "version": 1,
                    "state": "unprocessed",
                    "amount": "-10.00",
                    "amount_open": "-10.00",
                    "payments": [],
                    "ledger_account_bookings": [],
                }

            def get_financial_mutation(self, _mutation_id):
                return copy.deepcopy(self.record)

            def get_ledger_account(self, ledger_id):
                return {
                    "id": ledger_id,
                    "version": self.target_version,
                    "name": "Target",
                    "account_type": "expenses",
                    "active": True,
                }

            def link_financial_mutation_booking(self, _mutation_id, _booking):
                self.link_calls += 1

        client = Client()
        with self._patch_client(bank, client):
            prepared = bank.prepare_link_bank_mutation_booking(
                "mutation-1",
                "LedgerAccount",
                "ledger-1",
                "-10.00",
            )
            client.target_version = 2
            with self.assertRaisesRegex(MoneybirdError, "target changed"):
                bank.link_bank_mutation_booking_from_approval(
                    prepared["approval_id"]
                )

        self.assertEqual(client.link_calls, 0)
        state = safety.approval_execution_state(
            prepared["approval_id"],
            administration_id=client.administration_id,
        )
        self.assertEqual(state["state"], "failed_pre_write")
        self.assertEqual(state["phase"], "completed")

    def test_operator_reconciliation_unlocks_ambiguous_fingerprint(self) -> None:
        class Client:
            administration_id = "contract-admin"

        fingerprint = "ambiguous-occurrence"
        first = safety.make_approval(
            "recovery_demo",
            {"fingerprint": fingerprint},
            "recovery",
        )

        def ambiguous(_client, _payload):
            mark_write_dispatch_started()
            raise MoneybirdError("simulated timeout")

        with self.assertRaisesRegex(MoneybirdError, "simulated timeout"):
            run_approved_write(
                Client(),
                first["approval_id"],
                "recovery_demo",
                ambiguous,
            )
        second = safety.make_approval(
            "recovery_demo",
            {"fingerprint": fingerprint},
            "retry after proof",
        )
        with self.assertRaisesRegex(MoneybirdError, "requires reconciliation"):
            safety.pop_approval(
                second["approval_id"],
                "recovery_demo",
                administration_id=Client.administration_id,
            )

        reconciled = safety.reconcile_approval_execution(
            first["approval_id"],
            "proven_absent",
            evidence="Provider request log proves no mutation was accepted.",
            reconciled_by="test-operator",
            administration_id=Client.administration_id,
        )
        self.assertEqual(reconciled["state"], "reconciled_absent")
        self.assertEqual(reconciled["outcome"], "reconciled_absent")
        self.assertEqual(reconciled["phase"], "reconciled")
        claimed = safety.pop_approval(
            second["approval_id"],
            "recovery_demo",
            administration_id=Client.administration_id,
        )
        self.assertEqual(claimed["approval_id"], second["approval_id"])


if __name__ == "__main__":
    unittest.main()
