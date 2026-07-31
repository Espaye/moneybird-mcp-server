from __future__ import annotations

import copy
import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird import safety
from moneybird.config import MoneybirdError
from moneybird.credentials import set_active_administration_id
from moneybird.tools import workflows
from moneybird.tools.approvals import APPROVAL_EXECUTORS
from moneybird.tools.workflows import _preflight_workflow_children


class WorkflowSafetyTests(unittest.TestCase):
    class FakeClient:
        administration_id = "workflow-admin"

        def __init__(self) -> None:
            self.write_calls = 0
            self.mutation = {
                "id": "mutation-1",
                "version": 7,
                "state": "processed",
                "amount": "-10.00",
                "amount_open": "0.00",
                "ledger_account_bookings": [
                    {
                        "id": "booking-1",
                        "ledger_account_id": "ledger-source",
                        "price": "-10.00",
                        "description": None,
                    }
                ],
            }
            self.document = {
                "id": "document-1",
                "version": 2,
                "updated_at": "2026-01-02T00:00:00Z",
                "total_price_incl_tax": "10.00",
            }

        def list_ledger_accounts(self):
            return [
                {"id": "ledger-source", "active": True},
                {"id": "ledger-target", "active": True},
            ]

        def fetch_financial_mutations_by_ids(self, ids):
            return [copy.deepcopy(self.mutation)]

        def get_financial_mutation(self, item_id):
            return copy.deepcopy(self.mutation)

        def fetch_documents_by_ids(self, kind, ids):
            return [copy.deepcopy(self.document)]

        def get_document(self, kind, item_id):
            return copy.deepcopy(self.document)

    def tearDown(self) -> None:
        safety.clear_pending_approvals()

    def test_every_guarded_action_has_generic_dispatch(self) -> None:
        self.assertIn("bookkeeping_correction_batch", APPROVAL_EXECUTORS)
        self.assertIn("reconcile_purchase_invoice", APPROVAL_EXECUTORS)
        self.assertIn("reclassify_bank_mutation_bookings", APPROVAL_EXECUTORS)
        self.assertGreaterEqual(len(APPROVAL_EXECUTORS), 21)

    def test_mixed_workflow_preflights_all_children_before_writes(self) -> None:
        client = self.FakeClient()
        set_active_administration_id(client.administration_id)
        bank_payload = {
            "items": [
                {
                    "financial_mutation_id": "mutation-1",
                    "expected_version": "7",
                    "expected_state": "processed",
                    "expected_amount": "-10.00",
                    "expected_amount_open": "0.00",
                    "source_booking": {
                        "id": "booking-1",
                        "ledger_account_id": "ledger-source",
                        "price": "-10.00",
                        "description": None,
                    },
                    "target_ledger_account_id": "ledger-target",
                    "target_ledger_account_name": "Private",
                }
            ]
        }
        # Deliberately stale version: the whole mixed workflow must abort
        # before any child executor can write.
        purchase_payload = {
            "document_kind": "purchase_invoice",
            "document_id": "document-1",
            "expected_version": "1",
            "expected_updated_at": "2026-01-01T00:00:00Z",
            "expected_total_incl_tax": "10.00",
            "expected_total_before": "10.00",
        }
        bank_approval = safety.make_approval(
            "reclassify_bank_mutation_bookings",
            bank_payload,
            "bank",
        )
        purchase_approval = safety.make_approval(
            "reconcile_purchase_invoice",
            purchase_payload,
            "purchase",
        )
        children = [
            {
                "approval_id": bank_approval["approval_id"],
                "action": "reclassify_bank_mutation_bookings",
                "payload": bank_payload,
            },
            {
                "approval_id": purchase_approval["approval_id"],
                "action": "reconcile_purchase_invoice",
                "payload": purchase_payload,
            },
        ]

        with self.assertRaises(MoneybirdError):
            _preflight_workflow_children(client, children)
        self.assertEqual(client.write_calls, 0)

    def test_parent_reports_child_verification_failure(self) -> None:
        client = self.FakeClient()
        set_active_administration_id(client.administration_id)
        child_payload = {"items": []}
        child = safety.make_approval(
            "reclassify_bank_mutation_bookings",
            child_payload,
            "child",
        )
        parent_payload = {
            "children": [
                {
                    "approval_id": child["approval_id"],
                    "action": "reclassify_bank_mutation_bookings",
                    "payload": child_payload,
                }
            ],
            "fingerprint": "workflow-partial",
        }
        parent = safety.make_approval(
            "bookkeeping_correction_batch",
            parent_payload,
            "parent",
        )

        def partial_child(approval_id: str):
            safety.pop_approval(
                approval_id,
                "reclassify_bank_mutation_bookings",
                administration_id=client.administration_id,
            )
            return {
                "status": "completed_with_errors",
                "fully_verified": False,
            }

        with (
            mock.patch.object(workflows.ctx, "get_client", return_value=client),
            mock.patch.dict(
                workflows._WORKFLOW_EXECUTORS,
                {"reclassify_bank_mutation_bookings": partial_child},
                clear=True,
            ),
        ):
            result = workflows.bookkeeping_correction_batch_from_approval(
                parent["approval_id"]
            )

        self.assertEqual(result["status"], "completed_with_errors")
        self.assertFalse(result["fully_verified"])
        self.assertEqual(result["completed"], [])
        self.assertEqual(len(result["failures"]), 1)
        self.assertEqual(
            result["failures"][0]["result"]["status"],
            "completed_with_errors",
        )
        self.assertFalse(
            safety.audit_log_contains_success(
                "bookkeeping_correction_batch",
                "workflow-partial",
                administration_id=client.administration_id,
            )
        )


if __name__ == "__main__":
    unittest.main()
