from __future__ import annotations

import os
import unittest
from unittest import mock

from moneybird_mcp.capabilities import (
    CAPABILITY_MODE_ENV,
    CapabilityMode,
    capability_mode,
    require_write_capability,
    writes_enabled,
)
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import CREDENTIAL_MODE_ENV, set_active_administration_id
from moneybird_mcp.safety import make_approval


class CapabilityPolicyTests(unittest.TestCase):
    def test_default_is_read_only(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(capability_mode(), CapabilityMode.READ_ONLY)
            self.assertFalse(writes_enabled())
            with self.assertRaisesRegex(MoneybirdError, "writes are disabled"):
                require_write_capability(action="create_contact")

    def test_write_mode_requires_explicit_value(self) -> None:
        with mock.patch.dict(
            os.environ,
            {CAPABILITY_MODE_ENV: "write_enabled"},
            clear=True,
        ):
            self.assertEqual(capability_mode(), CapabilityMode.WRITE_ENABLED)
            self.assertTrue(writes_enabled())
            require_write_capability(action="create_contact")

    def test_invalid_mode_fails_closed(self) -> None:
        with mock.patch.dict(
            os.environ,
            {CAPABILITY_MODE_ENV: "maybe"},
            clear=True,
        ):
            with self.assertRaisesRegex(MoneybirdError, CAPABILITY_MODE_ENV):
                capability_mode()

    def test_hosted_mode_cannot_enable_writes_with_process_environment(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                CAPABILITY_MODE_ENV: "write_enabled",
                CREDENTIAL_MODE_ENV: "hosted_request_only",
            },
            clear=True,
        ):
            self.assertTrue(writes_enabled())
            with self.assertRaisesRegex(MoneybirdError, "hosted_request_only"):
                require_write_capability(action="create_contact")

    def test_hosted_mode_cannot_persist_write_preparation(self) -> None:
        set_active_administration_id("123")
        self.addCleanup(set_active_administration_id, None)
        with mock.patch.dict(
            os.environ,
            {CREDENTIAL_MODE_ENV: "hosted_request_only"},
            clear=False,
        ):
            with self.assertRaisesRegex(MoneybirdError, "Write preparation"):
                make_approval("create_contact", {"company_name": "Example"}, "demo")

    def test_hosted_generic_executor_rejects_before_approval_state_access(self) -> None:
        from moneybird_mcp.tools import approvals

        with (
            mock.patch.dict(
                os.environ,
                {
                    CAPABILITY_MODE_ENV: "write_enabled",
                    CREDENTIAL_MODE_ENV: "hosted_request_only",
                },
                clear=True,
            ),
            mock.patch.object(approvals, "peek_approval") as peek,
            mock.patch.object(approvals.ctx, "get_client") as get_client,
        ):
            with self.assertRaisesRegex(MoneybirdError, "hosted_request_only"):
                approvals.execute_approved_action("approval-id")

        peek.assert_not_called()
        get_client.assert_not_called()
