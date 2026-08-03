from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from moneybird_mcp import safety
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import contacts


class ContactWriteSafetyTests(unittest.TestCase):
    class FakeClient:
        administration_id = "fresh-admin"

        def __init__(self) -> None:
            self.record = {
                "id": "123",
                "email": "before@example.com",
                "company_name": "Example",
                "version": 4,
                "updated_at": "2026-07-30T10:00:00Z",
            }
            self.update_calls = 0

        def get_contact(self, contact_id: str):
            assert contact_id == "123"
            return dict(self.record)

        def update_contact(self, contact_id: str, values):
            assert contact_id == "123"
            self.update_calls += 1
            self.record.update(values)
            self.record["version"] += 1
            self.record["updated_at"] = "2026-07-30T10:01:00Z"
            return dict(self.record)

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="moneybird_contact_write_"
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

    def test_prepare_binds_fresh_client_and_shows_before_after(self) -> None:
        fake = self.FakeClient()
        set_active_administration_id("stale-admin")

        def resolve_client():
            set_active_administration_id(fake.administration_id)
            return fake

        with mock.patch.object(
            contacts.ctx,
            "get_client",
            side_effect=resolve_client,
        ) as get_client:
            prepared = contacts.prepare_update_contact(
                "123",
                email="after@example.com",
            )

        get_client.assert_called_once_with()
        self.assertEqual(
            prepared["preview"]["before"]["email"],
            "before@example.com",
        )
        self.assertEqual(
            prepared["preview"]["after"]["email"],
            "after@example.com",
        )
        pending = safety.peek_approval(
            prepared["approval_id"],
            administration_id="fresh-admin",
        )
        self.assertEqual(pending["administration_id"], "fresh-admin")

    def test_update_requires_precondition_and_independent_post_read(self) -> None:
        fake = self.FakeClient()

        def resolve_client():
            set_active_administration_id(fake.administration_id)
            return fake

        with mock.patch.object(
            contacts.ctx,
            "get_client",
            side_effect=resolve_client,
        ):
            prepared = contacts.prepare_update_contact(
                "123",
                email="after@example.com",
            )
            result = contacts.update_contact_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(fake.update_calls, 1)
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["verification"]["independent_post_read"])
        self.assertTrue(result["verification"]["requested_fields_match"])


if __name__ == "__main__":
    unittest.main()
