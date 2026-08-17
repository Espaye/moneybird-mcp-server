from __future__ import annotations

import copy
import os
import tempfile
import unittest
from unittest import mock

from moneybird_mcp import safety
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import bank, ledger, payments, sales, sales_batches
from moneybird_mcp.tools._writes import (
    mark_write_dispatch_started,
    run_approved_write,
)
from moneybird_mcp.tools.approvals import APPROVAL_EXECUTORS
from moneybird_mcp.write_contracts import WRITE_SPECS


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

    def test_journal_description_rides_on_lines_because_moneybird_drops_it(self) -> None:
        # Moneybird stores no header description on a general journal document; the
        # field is absent from the returned record. Sending one made every journal
        # created with a description fail its own post-write verifier.
        sent: dict = {}

        class Client:
            administration_id = "contract-admin"

            def list_ledger_accounts(self):
                return [{"id": "a", "name": "A"}, {"id": "b", "name": "B"}]

            def create_general_journal_document(self, payload):
                sent.update(payload)
                return {"id": "journal-1"}

            def get_document(self, _kind, _document_id):
                return {
                    "id": "journal-1",
                    "reference": "R",
                    "date": "2026-07-01",
                    "general_journal_document_entries": [
                        {
                            "ledger_account_id": "a",
                            "debit": "10.0",
                            "credit": "0.0",
                            "description": "D",
                        },
                        {
                            "ledger_account_id": "b",
                            "debit": "0.0",
                            "credit": "10.0",
                            "description": "own text",
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
                    {
                        "ledger_account_id": "b",
                        "debit": "0",
                        "credit": "10",
                        "description": "own text",
                    },
                ],
                "D",
            )
            result = ledger.create_general_journal_document_from_approval(
                prepared["approval_id"]
            )

        self.assertNotIn("description", sent)
        lines = sent["general_journal_document_entries_attributes"]
        # The shared text fills only the line that had none of its own.
        self.assertEqual(lines["0"]["description"], "D")
        self.assertEqual(lines["1"]["description"], "own text")
        self.assertEqual(result["verification"]["field_mismatches"], {})
        self.assertTrue(result["verification"]["fully_verified"])

    def test_wrong_invoice_line_with_same_count_is_not_verified(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def get_contact(self, _contact_id):
                return {"id": "contact-1"}

            def list_sales_invoices(self, **_kwargs):
                return []

            def list_tax_rates(self):
                return [{"id": "tax-21", "percentage": "21"}]

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
                        "tax_rate_id": "tax-21",
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

    def test_batch_update_rejects_ignored_fields_and_empty_patches(self) -> None:
        class Client:
            administration_id = "contract-admin"

            def __init__(self) -> None:
                self.update_calls = 0

            def update_sales_invoice(self, _invoice_id, _patch):
                self.update_calls += 1

        client = Client()
        with self._patch_client(sales_batches, client):
            with self.assertRaisesRegex(MoneybirdError, "Use new_reference"):
                sales_batches.prepare_batch_update_sales_invoices(
                    [
                        {
                            "sales_invoice_id": "invoice-1",
                            "reference": "MCP testfactuur v2",
                        }
                    ]
                )
            with self.assertRaisesRegex(MoneybirdError, "unsupported field"):
                sales_batches.prepare_batch_update_sales_invoices(
                    [{"sales_invoice_id": "invoice-1", "bogus": "value"}]
                )

            # Defensive execution check for approvals created by an older server.
            legacy = safety.make_approval(
                "batch_update_sales_invoices",
                {
                    "items": [
                        {
                            "sales_invoice_id": "invoice-1",
                            "patch": {},
                            "precondition": {},
                        }
                    ],
                    "fingerprint": "legacy-empty-batch-update",
                },
                "legacy empty batch update",
            )
            with self.assertRaisesRegex(MoneybirdError, "empty patch"):
                sales_batches.batch_update_sales_invoices_from_approval(
                    legacy["approval_id"]
                )

        self.assertEqual(client.update_calls, 0)

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

            def get_ledger_account(self, ledger_account_id):
                return {"id": ledger_account_id, "name": "Ledger one"}

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
            self.assertIn(
                "creates no VAT posting",
                " ".join(prepared["preview"]["warnings"]),
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


class LinkBooksUnbookedDocumentThroughTests(unittest.TestCase):
    """End-to-end: linking a payment must finish an unbooked document.

    Covers the caller side of the book-through, which the unit tests around
    _book_document_through cannot see: which outcomes are folded into the
    action's verification, and which are correct outcomes that must not be.
    """

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="moneybird_book_through_"
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

    class _Client:
        administration_id = "contract-admin"

        def __init__(self, *, total_unpaid="0.00", total_after="25.99"):
            self._total_unpaid = total_unpaid
            self._total_after = total_after
            self._saved = False
            self.update_calls = 0
            self.mutation = {
                "id": "mutation-1",
                "version": 1,
                "state": "unprocessed",
                "amount": "-25.99",
                "amount_open": "-25.99",
                "payments": [],
                "ledger_account_bookings": [],
            }

        def get_financial_mutation(self, _mutation_id):
            return copy.deepcopy(self.mutation)

        def get_document(self, kind, document_id):
            if kind != "purchase_invoice":
                raise MoneybirdError("not a receipt")
            return {
                "id": document_id,
                "version": 7,
                "state": "paid" if self._saved else "new",
                "currency": "EUR",
                "prices_are_incl_tax": False,
                "total_price_incl_tax": (
                    self._total_after if self._saved else "25.99"
                ),
                "total_unpaid": "0.00" if self._saved else self._total_unpaid,
                "details": [
                    {
                        "id": "line-1",
                        "description": "Kantoorbenodigdheden",
                        "price": "21.48",
                        "amount": "1",
                        "ledger_account_id": "led-1",
                        "tax_rate_id": "tax-1",
                        "row_order": 0,
                    }
                ],
                "contact": {"company_name": "Amazon EU SARL"},
            }

        def link_financial_mutation_booking(self, _mutation_id, _booking):
            self.mutation["payments"] = [
                {
                    "id": "payment-1",
                    "invoice_id": "doc-1",
                    "invoice_type": "Document",
                    "price": "25.99",
                }
            ]
            self.mutation["amount_open"] = "0.00"
            self.mutation["state"] = "processed"
            self.mutation["version"] += 1

        def update_document(self, _kind, _document_id, _body):
            self.update_calls += 1
            self._saved = True

    def _run(self, client):
        with self._patch_client(bank, client):
            prepared = bank.prepare_link_bank_mutation_booking(
                "mutation-1", "Document", "doc-1", "-25.99"
            )
            return prepared, bank.link_bank_mutation_booking_from_approval(
                prepared["approval_id"]
            )

    def test_a_fully_paid_new_document_is_booked_and_verified(self) -> None:
        client = self._Client()
        prepared, result = self._run(client)

        self.assertTrue(
            any("state 'new'" in w for w in prepared["preview"]["warnings"])
        )
        self.assertEqual(client.update_calls, 1)
        self.assertEqual(result["document"]["state_after"], "paid")
        self.assertTrue(result["verification"]["document_booked_out_of_new"])
        self.assertTrue(result["verification"]["document_total_unchanged"])
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(result["status"], "linked")

    def test_a_partial_payment_skips_the_save_without_failing_the_link(self) -> None:
        client = self._Client(total_unpaid="10.00")
        _prepared, result = self._run(client)

        self.assertEqual(client.update_calls, 0)
        self.assertIn("full open amount", result["document"]["skipped_reason"])
        # Skipping on purpose is a correct outcome, not a verification failure.
        self.assertNotIn("document_booked_out_of_new", result["verification"])
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(result["status"], "linked")

    def test_a_recalculated_total_fails_the_action(self) -> None:
        client = self._Client(total_after="30.00")
        _prepared, result = self._run(client)

        self.assertEqual(client.update_calls, 1)
        self.assertFalse(result["verification"]["document_total_unchanged"])
        self.assertFalse(result["verification"]["fully_verified"])
        self.assertEqual(result["status"], "completed_with_errors")
