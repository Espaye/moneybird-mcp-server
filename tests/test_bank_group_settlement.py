from __future__ import annotations

import copy
import os
import tempfile
import unittest
from decimal import Decimal
from unittest import mock

from moneybird_mcp import safety
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import approvals, bank


class GroupSettlementClient:
    administration_id = "group-settlement-admin"

    def __init__(self, *, fail_on: str = "") -> None:
        self.fail_on = fail_on
        self.link_calls: list[str] = []
        self.update_calls = 0
        self.invoice = {
            "id": "invoice-1",
            "reference": "MPDI260816662",
            "date": "2026-08-12",
            "state": "new",
            "paid_at": None,
            "version": 10,
            "prices_are_incl_tax": True,
            "total_price_incl_tax": "43.19",
            "contact": {"company_name": "Marktplaats B.V."},
            "details": [
                {
                    "id": "line-1",
                    "description": "Advertentieplaatsingen",
                    "price": "43.19",
                    "amount": "1",
                    "ledger_account_id": "ledger-ads",
                    "tax_rate_id": "tax-21",
                    "period": "20260701..20260731",
                }
            ],
            "payments": [],
        }
        self.mutations = {
            "m1": self._mutation("m1", "-11.50", 1),
            "m2": self._mutation("m2", "-13.50", 2),
            "m3": self._mutation("m3", "-18.19", 3),
        }

    @staticmethod
    def _mutation(item_id: str, amount: str, version: int) -> dict:
        return {
            "id": item_id,
            "date": "2026-07-28",
            "state": "unprocessed",
            "amount": amount,
            "amount_open": amount,
            "version": version,
            "contra_account_name": "Marktplaats B.V.",
            "payments": [],
            "ledger_account_bookings": [],
        }

    def get_document(self, _kind: str, _document_id: str) -> dict:
        return copy.deepcopy(self.invoice)

    def fetch_documents_by_ids(self, _kind: str, _ids: list[str]) -> list[dict]:
        return [copy.deepcopy(self.invoice)]

    def get_financial_mutation(self, mutation_id: str) -> dict:
        return copy.deepcopy(self.mutations[mutation_id])

    def fetch_financial_mutations_by_ids(self, ids: list[str]) -> list[dict]:
        return [copy.deepcopy(self.mutations[item_id]) for item_id in ids]

    def link_financial_mutation_booking(self, mutation_id: str, booking: dict) -> dict:
        self.link_calls.append(mutation_id)
        if mutation_id == self.fail_on:
            raise MoneybirdError(f"synthetic link failure for {mutation_id}")
        mutation = self.mutations[mutation_id]
        payment = {
            "id": f"payment-{mutation_id}",
            "invoice_id": booking["booking_id"],
            "invoice_type": "Document",
            "financial_mutation_id": mutation_id,
            # Moneybird returns invoice payments as positive magnitudes.
            "price": f"{abs(Decimal(booking['price'])):.2f}",
        }
        mutation["payments"] = [payment]
        mutation["state"] = "processed"
        mutation["amount_open"] = "0.00"
        mutation["version"] += 1
        self.invoice["payments"].append(copy.deepcopy(payment))
        self.invoice["version"] += 1
        return copy.deepcopy(mutation)

    def update_document(self, _kind: str, _document_id: str, patch: dict) -> dict:
        self.update_calls += 1
        self.invoice["state"] = "paid"
        self.invoice["paid_at"] = "2026-07-28"
        self.invoice["version"] += 1
        return copy.deepcopy(self.invoice)


class GroupSettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_group_settlement_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id(GroupSettlementClient.administration_id)

    def tearDown(self) -> None:
        safety.clear_pending_approvals()
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    @staticmethod
    def _prepare(client: GroupSettlementClient) -> dict:
        with mock.patch.object(bank.ctx, "get_client", return_value=client):
            return bank.prepare_settle_purchase_invoice_from_bank_mutations(
                "invoice-1", ["m1", "m2", "m3"]
            )

    def test_one_approval_links_group_and_processes_invoice(self) -> None:
        client = GroupSettlementClient()
        prepared = self._prepare(client)

        self.assertEqual(prepared["preview"]["mutation_count"], 3)
        self.assertEqual(prepared["preview"]["total"], "43.19")
        self.assertEqual(
            prepared["preview"]["purchase_invoice"]["state_after_expected"],
            "paid",
        )

        with (
            mock.patch.object(bank.ctx, "get_client", return_value=client),
            mock.patch.object(approvals.ctx, "get_client", return_value=client),
        ):
            result = approvals.execute_approved_action(prepared["approval_id"])

        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["fully_verified"])
        self.assertEqual(client.link_calls, ["m1", "m2", "m3"])
        self.assertEqual(client.update_calls, 1)
        self.assertEqual(client.invoice["state"], "paid")
        self.assertEqual(client.invoice["paid_at"], "2026-07-28")
        self.assertEqual(client.invoice["details"][0]["price"], "43.19")

    def test_stale_invoice_aborts_before_first_write(self) -> None:
        client = GroupSettlementClient()
        prepared = self._prepare(client)
        client.invoice["version"] += 1

        with (
            mock.patch.object(bank.ctx, "get_client", return_value=client),
            self.assertRaisesRegex(MoneybirdError, "invoice changed"),
        ):
            bank.settle_purchase_invoice_from_bank_mutations_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(client.link_calls, [])
        self.assertEqual(client.update_calls, 0)

    def test_link_failure_is_reported_as_partial_and_invoice_is_not_processed(self) -> None:
        client = GroupSettlementClient(fail_on="m2")
        prepared = self._prepare(client)

        with mock.patch.object(bank.ctx, "get_client", return_value=client):
            result = bank.settle_purchase_invoice_from_bank_mutations_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertFalse(result["fully_verified"])
        self.assertEqual(client.link_calls, ["m1", "m2"])
        self.assertEqual(client.update_calls, 0)
        self.assertEqual(result["failures"][0]["financial_mutation_id"], "m2")

    def test_inexact_or_mixed_supplier_groups_are_rejected(self) -> None:
        for name, ids, error in (
            ("total", ["m1", "m2"], "closed exactly"),
            ("supplier", ["m1", "m2", "m3"], "supplier group"),
        ):
            client = GroupSettlementClient()
            if name == "supplier":
                client.mutations["m2"]["contra_account_name"] = "Andere leverancier"
            with self.subTest(name=name), mock.patch.object(
                bank.ctx, "get_client", return_value=client
            ), self.assertRaisesRegex(MoneybirdError, error):
                bank.prepare_settle_purchase_invoice_from_bank_mutations(
                    "invoice-1", ids
                )

    def test_outgoing_document_payment_uses_positive_returned_magnitude(self) -> None:
        client = GroupSettlementClient()
        client.invoice["total_price_incl_tax"] = "11.50"
        with mock.patch.object(bank.ctx, "get_client", return_value=client):
            prepared = bank.prepare_link_bank_mutation_booking(
                "m1", "Document", "invoice-1"
            )
            result = bank.link_bank_mutation_booking_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "linked")
        self.assertTrue(result["verification"]["new_link_price_matches"])


if __name__ == "__main__":
    unittest.main()


class BookDocumentThroughTests(unittest.TestCase):
    """A linked payment must not leave the document unbooked.

    Linking settles the money; the document stays in state 'new' until it is
    saved. The grouped settlement path already finishes that job, so the
    single-mutation path does too — otherwise two tools that both "link a
    payment to an invoice" leave different states for no reason a caller can
    see. That asymmetry left a paid invoice sitting in 'new' in production.
    """

    class _Client:
        def __init__(
            self,
            *,
            state="new",
            open_amount="0.00",
            fail=None,
            fail_read_after_save=False,
            fail_first_read=False,
            total_after="25.99",
            drop_line_after=False,
        ):
            self._state = state
            self._open_amount = open_amount
            self._fail = fail
            self._fail_read_after_save = fail_read_after_save
            self._fail_first_read = fail_first_read
            self._total_after = total_after
            self._drop_line_after = drop_line_after
            self._saved = False
            self.update_calls: list[dict] = []

        def get_document(self, kind, document_id):
            if self._fail_first_read and not self._saved:
                raise MoneybirdError("read failed before the save")
            if self._fail_read_after_save and self._saved:
                raise MoneybirdError("read timed out after the save")
            details = [
                {
                    "id": "line-1",
                    "description": "Kantoorbenodigdheden",
                    "price": "21.48",
                    "amount": "1",
                    "ledger_account_id": "led-1",
                    "tax_rate_id": "tax-1",
                    "row_order": 0,
                }
            ]
            if self._saved and self._drop_line_after:
                details[0]["ledger_account_id"] = "led-CHANGED"
            return {
                "id": document_id,
                "state": self._state,
                "total_price_incl_tax": (
                    self._total_after if self._saved else "25.99"
                ),
                "total_price_paid": self._paid(),
                "payments": [{"price": self._paid()}],
                "details": details,
            }

        def _paid(self):
            return f"{Decimal('25.99') - Decimal(self._open_amount):.2f}"

        def update_document(self, kind, document_id, body):
            if self._fail is not None:
                raise self._fail
            self.update_calls.append(body)
            self._saved = True
            self._state = "paid"

    @staticmethod
    def _payload():
        return {
            "book_through": {
                "document_kind": "purchase_invoice",
                "document_id": "doc-1",
                "prices_are_incl_tax": False,
                "lines": [
                    {
                        "id": "line-1",
                        "description": "Kantoorbenodigdheden",
                        "price": "21.48",
                        "amount": "1",
                    }
                ],
            }
        }

    def test_a_fully_paid_new_document_is_booked_through(self) -> None:
        client = self._Client()
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertTrue(outcome["attempted"])
        self.assertEqual(outcome["state_after"], "paid")
        self.assertTrue(outcome["total_unchanged"])
        self.assertEqual(len(client.update_calls), 1)

    def test_a_partially_paid_document_is_left_in_new(self) -> None:
        client = self._Client(open_amount="10.00")
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertFalse(outcome["attempted"])
        self.assertIn("full open amount", outcome["skipped_reason"])
        self.assertEqual(client.update_calls, [])

    def test_an_unverified_link_never_touches_the_document(self) -> None:
        client = self._Client()
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=False
        )
        self.assertFalse(outcome["attempted"])
        self.assertEqual(client.update_calls, [])

    def test_an_already_booked_document_is_not_saved_again(self) -> None:
        client = self._Client(state="paid")
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertEqual(outcome["state_after"], "paid")
        self.assertEqual(client.update_calls, [])

    def test_a_refused_save_is_reported_as_still_new(self) -> None:
        from moneybird_mcp.config import MoneybirdHTTPError

        client = self._Client(
            fail=MoneybirdHTTPError("refused", status_code=422)
        )
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertIn("error", outcome)
        # Moneybird answered "no", so nothing was applied: 'new' is a fact here.
        self.assertEqual(outcome["state_after"], "new")
        self.assertNotIn("verification_gap", outcome)

    def test_an_ambiguous_save_failure_is_not_reported_as_new(self) -> None:
        client = self._Client(fail=MoneybirdError("connection reset"))
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertEqual(outcome["state_after"], "unknown")
        self.assertIn("verification_gap", outcome)

    def test_a_read_failing_after_the_save_leaves_the_state_unknown(self) -> None:
        client = self._Client(fail_read_after_save=True)
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        # The save landed; only the confirming read died. Calling it 'new' would
        # report a finished invoice as unfinished.
        self.assertEqual(outcome["state_after"], "unknown")
        self.assertIn("verification_gap", outcome)
        self.assertEqual(len(client.update_calls), 1)

    def test_a_read_failing_before_the_save_is_still_new(self) -> None:
        client = self._Client(fail_first_read=True)
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertEqual(outcome["state_after"], "new")
        self.assertEqual(client.update_calls, [])

    def test_a_recalculated_total_is_reported_as_changed(self) -> None:
        client = self._Client(total_after="30.00")
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertFalse(outcome["total_unchanged"])

    def test_altered_booking_fields_are_reported_as_changed(self) -> None:
        client = self._Client(drop_line_after=True)
        outcome = bank._book_document_through(
            client, self._payload(), link_verified=True
        )
        self.assertTrue(outcome["total_unchanged"])
        self.assertFalse(outcome["lines_unchanged"])

    def test_nothing_to_book_returns_nothing(self) -> None:
        self.assertIsNone(
            bank._book_document_through(self._Client(), {}, link_verified=True)
        )
