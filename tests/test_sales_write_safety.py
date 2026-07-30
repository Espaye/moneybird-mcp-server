from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from moneybird.credentials import set_active_administration_id
from moneybird.tools import sales


class SalesWorkflowWriteSafetyTests(unittest.TestCase):
    class FakeClient:
        administration_id = "sales-admin"

        def __init__(self) -> None:
            self.record = {
                "id": "123",
                "invoice_id": "2026-001",
                "state": "scheduled",
                "paused": False,
                "invoice_date": "2026-08-01",
                "version": 1,
                "updated_at": "2026-07-30T10:00:00Z",
            }
            self.pause_calls = 0
            self.resume_calls = 0

        def get_sales_invoice(self, sales_invoice_id: str):
            assert sales_invoice_id == "123"
            return dict(self.record)

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


if __name__ == "__main__":
    unittest.main()
