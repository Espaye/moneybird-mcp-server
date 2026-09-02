"""Guarded ledger-account maintenance: taxonomy correction and empty-test cleanup.

Both actions edit reference data every other tool resolves against, and one of
them is destructive, so the tests here drive the real approval kernel rather
than calling the executors directly: prepare stages an approval, the public
``*_from_approval`` entry point claims it, and only then does anything reach the
fake provider. That is the same path ``execute_approved_action`` takes, so a
regression in binding, single-use claiming, duplicate suppression or capability
policy shows up here instead of in production.

Every identifier, name and date below is synthesized.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from unittest import mock

from moneybird_mcp import safety
from moneybird_mcp.config import MoneybirdError, MoneybirdHTTPError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import ledger

ADMINISTRATION = "ledger-maintenance-admin"
OTHER_ADMINISTRATION = "unrelated-admin"
LEDGER_ID = "44001"


def _recent_first_of_month(months_back: int = 1) -> date:
    """A month start close enough to today to stay inside the 12-month evidence window.

    Pinning a literal date here would make the empty-ledger tests start failing
    on a calendar boundary rather than on a code change.
    """
    cursor = date.today().replace(day=1)
    for _ in range(months_back):
        cursor = (cursor - timedelta(days=1)).replace(day=1)
    return cursor


class _LedgerAccountClient:
    """Minimal provider double: one ledger account, optional assets and entries."""

    def __init__(self, *, administration_id: str = ADMINISTRATION) -> None:
        self.administration_id = administration_id
        self.created_at = _recent_first_of_month().isoformat() + "T09:00:00.000Z"
        self.record = {
            "id": LEDGER_ID,
            "name": "Sandbox Verrekenrekening",
            "account_type": "expenses",
            "account_id": "48800",
            "parent_id": None,
            "active": True,
            "created_at": self.created_at,
            "updated_at": self.created_at,
            "taxonomy_item": {"code": "WBedOvbOvb"},
        }
        self.assets: list[dict] = []
        self.entries: list[dict] = []
        self.patch_calls: list[tuple] = []
        self.delete_calls: list[str] = []
        self.deleted = False
        self.deactivate_instead = False

    # -- reads ---------------------------------------------------------------
    def get_ledger_account(self, ledger_account_id):
        if self.deleted and not self.deactivate_instead:
            raise MoneybirdHTTPError("not found", status_code=404)
        if self.deleted and self.deactivate_instead:
            return {**self.record, "active": False}
        return dict(self.record)

    def list_all_assets(self, *, active=None):
        return [dict(item) for item in self.assets]

    def get_report(self, report_name, *, period, page=None, extra_query=None):
        assert report_name == "journal_entries"
        return [dict(item) for item in self.entries]

    # -- writes --------------------------------------------------------------
    def update_ledger_account(self, ledger_account_id, payload=None, *, rgs_code=""):
        self.patch_calls.append((ledger_account_id, payload, rgs_code))
        if payload and payload.get("name"):
            self.record["name"] = payload["name"]
        if rgs_code:
            self.record["taxonomy_item"] = {"code": rgs_code}
        self.record["updated_at"] = "2026-01-01T00:00:00.000Z"
        return dict(self.record)

    def delete_ledger_account(self, ledger_account_id):
        self.delete_calls.append(ledger_account_id)
        self.deleted = True


class _IsolatedWriteKernel(unittest.TestCase):
    """Give each test its own approval store, audit log and active administration."""

    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_ledger_maint_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
            },
        )
        self._env.start()
        set_active_administration_id(ADMINISTRATION)
        safety.clear_pending_approvals()

    def tearDown(self) -> None:
        safety.clear_pending_approvals()
        set_active_administration_id(None)
        self._env.stop()
        self._temp_dir.cleanup()

    def _with_client(self, client):
        return mock.patch.object(ledger.ctx, "get_client", return_value=client)


class LedgerAccountUpdateTests(_IsolatedWriteKernel):
    def _prepare(self, client, **overrides):
        kwargs = {"ledger_account_id": LEDGER_ID, "rgs_code": "WBedOvbAlg"}
        kwargs.update(overrides)
        with self._with_client(client):
            return ledger.prepare_update_ledger_account(**kwargs)

    def test_prepare_stages_an_approval_and_writes_nothing(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)

        self.assertEqual(client.patch_calls, [])
        self.assertEqual(prepared["preview"]["changes"]["rgs_code"], "WBedOvbAlg")
        self.assertEqual(prepared["preview"]["before"]["rgs_code"], "WBedOvbOvb")
        pending = safety.peek_approval(
            prepared["approval_id"], administration_id=ADMINISTRATION
        )
        self.assertEqual(pending["administration_id"], ADMINISTRATION)

    def test_approved_update_patches_once_and_verifies_independently(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client, name="Sandbox Tussenrekening")

        with self._with_client(client):
            result = ledger.update_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(len(client.patch_calls), 1)
        self.assertEqual(
            client.patch_calls[0],
            (LEDGER_ID, {"name": "Sandbox Tussenrekening"}, "WBedOvbAlg"),
        )
        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["verification"]["independent_post_read"])
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(result["verification"]["rgs_code_actual"], "WBedOvbAlg")
        self.assertEqual(
            result["verification"]["name_actual"], "Sandbox Tussenrekening"
        )
        self.assertTrue(all(result["verification"]["unchanged_fields"].values()))

    def test_the_approval_is_single_use(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)

        with self._with_client(client):
            ledger.update_ledger_account_from_approval(prepared["approval_id"])
            with self.assertRaises(MoneybirdError):
                ledger.update_ledger_account_from_approval(prepared["approval_id"])

        self.assertEqual(len(client.patch_calls), 1)

    def test_an_identical_repeat_is_suppressed_by_the_audit_log(self) -> None:
        client = _LedgerAccountClient()
        first = self._prepare(client)
        with self._with_client(client):
            ledger.update_ledger_account_from_approval(first["approval_id"])

        # Same target, same requested taxonomy: a fresh approval must not become
        # a second PATCH just because the user previewed it twice.
        client.record["taxonomy_item"] = {"code": "WBedOvbOvb"}
        client.record["updated_at"] = client.created_at
        second = self._prepare(client)
        with self._with_client(client):
            with self.assertRaisesRegex(MoneybirdError, "already completed"):
                ledger.update_ledger_account_from_approval(second["approval_id"])

        self.assertEqual(len(client.patch_calls), 1)

    def test_a_stale_preview_aborts_before_any_write(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)
        client.record["name"] = "Renamed by somebody else"

        with self._with_client(client):
            result = ledger.update_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(client.patch_calls, [])
        self.assertEqual(result["status"], "precondition_failed")
        self.assertIn("changed after preview", result["error"])

    def test_an_approval_cannot_execute_against_another_administration(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)
        other = _LedgerAccountClient(administration_id=OTHER_ADMINISTRATION)

        with self._with_client(other):
            with self.assertRaises(MoneybirdError):
                ledger.update_ledger_account_from_approval(prepared["approval_id"])

        self.assertEqual(other.patch_calls, [])
        self.assertEqual(client.patch_calls, [])

    def test_read_only_mode_refuses_and_leaves_the_approval_usable(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)

        with mock.patch.dict(
            os.environ, {"MONEYBIRD_CAPABILITY_MODE": "read_only"}
        ):
            with self._with_client(client):
                with self.assertRaisesRegex(MoneybirdError, "writes are disabled"):
                    ledger.update_ledger_account_from_approval(
                        prepared["approval_id"]
                    )
        self.assertEqual(client.patch_calls, [])

        with self._with_client(client):
            result = ledger.update_ledger_account_from_approval(
                prepared["approval_id"]
            )
        self.assertEqual(result["status"], "updated")

    def test_required_arguments_are_refused_empty(self) -> None:
        client = _LedgerAccountClient()
        with self._with_client(client):
            with self.assertRaisesRegex(MoneybirdError, "ledger_account_id"):
                ledger.prepare_update_ledger_account(
                    ledger_account_id="  ", rgs_code="WBedOvbAlg"
                )
            with self.assertRaisesRegex(MoneybirdError, "rgs_code"):
                ledger.prepare_update_ledger_account(
                    ledger_account_id=LEDGER_ID, rgs_code=" "
                )
        self.assertEqual(client.patch_calls, [])

    def test_a_taxonomy_only_patch_keeps_the_wrapper_the_provider_demands(self) -> None:
        from moneybird_mcp.client import MoneybirdClient

        client = MoneybirdClient(token="synthetic-token", administration_id="9001")
        with mock.patch.object(
            client, "_request", return_value={"id": LEDGER_ID}
        ) as request:
            client.update_ledger_account(LEDGER_ID, rgs_code="WBedOvbAlg")

        request.assert_called_once_with(
            "PATCH",
            f"/9001/ledger_accounts/{LEDGER_ID}.json",
            body={"ledger_account": {}, "rgs_code": "WBedOvbAlg"},
        )

    def test_a_patch_with_neither_fields_nor_taxonomy_is_refused(self) -> None:
        from moneybird_mcp.client import MoneybirdClient

        client = MoneybirdClient(token="synthetic-token", administration_id="9001")
        with mock.patch.object(client, "_request") as request:
            with self.assertRaisesRegex(MoneybirdError, "ledger_account fields"):
                client.update_ledger_account(LEDGER_ID)
        request.assert_not_called()


class EmptyLedgerAccountCleanupTests(_IsolatedWriteKernel):
    def _prepare(self, client, **overrides):
        kwargs = {
            "ledger_account_id": LEDGER_ID,
            "expected_name": "Sandbox Verrekenrekening",
            "expected_created_date": client.created_at[:10],
            "test_provenance": "Created by the synthesized ledger-maintenance suite",
        }
        kwargs.update(overrides)
        with self._with_client(client):
            return ledger.prepare_delete_empty_ledger_account(**kwargs)

    def test_a_physical_delete_is_verified_by_a_404_read_back(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)
        self.assertTrue(prepared["preview"]["eligibility"]["balance_is_exact_zero"])
        self.assertEqual(client.delete_calls, [])

        with self._with_client(client):
            result = ledger.delete_empty_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(client.delete_calls, [LEDGER_ID])
        self.assertEqual(result["status"], "deleted")
        self.assertEqual(result["verification"]["provider_outcome"], "deleted")
        self.assertTrue(result["verification"]["independent_post_read"])
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(
            result["verification"]["asset_reference_count_before_delete"], 0
        )
        self.assertEqual(result["verification"]["journal_entry_count_before_delete"], 0)

    def test_a_provider_deactivation_counts_as_verified_removal(self) -> None:
        client = _LedgerAccountClient()
        client.deactivate_instead = True
        prepared = self._prepare(client)

        with self._with_client(client):
            result = ledger.delete_empty_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["verification"]["provider_outcome"], "deactivated")
        self.assertTrue(result["verification"]["fully_verified"])

    def test_journal_entries_block_the_preview(self) -> None:
        client = _LedgerAccountClient()
        client.entries = [{"id": "synthetic-entry"}]
        with self.assertRaisesRegex(MoneybirdError, "journal entries"):
            self._prepare(client)
        self.assertEqual(client.delete_calls, [])

    def test_an_asset_reference_blocks_the_preview(self) -> None:
        client = _LedgerAccountClient()
        client.assets = [
            {"id": "70001", "name": "Sandbox laptop", "ledger_account_id": LEDGER_ID}
        ]
        with self.assertRaisesRegex(MoneybirdError, "referenced"):
            self._prepare(client)
        self.assertEqual(client.delete_calls, [])

    def test_an_asset_on_another_ledger_does_not_block(self) -> None:
        client = _LedgerAccountClient()
        client.assets = [
            {"id": "70002", "name": "Sandbox printer", "ledger_account_id": "44999"}
        ]
        prepared = self._prepare(client)
        self.assertEqual(prepared["preview"]["eligibility"]["referencing_asset_count"], 0)

    def test_identity_guards_refuse_a_mismatched_target(self) -> None:
        client = _LedgerAccountClient()
        with self.assertRaisesRegex(MoneybirdError, "expected_name"):
            self._prepare(client, expected_name="Some other account")
        with self.assertRaisesRegex(MoneybirdError, "creation date"):
            self._prepare(client, expected_created_date="2020-01-01")
        with self.assertRaisesRegex(MoneybirdError, "test_provenance"):
            self._prepare(client, test_provenance="   ")
        self.assertEqual(client.delete_calls, [])

    def test_evidence_that_changed_after_the_preview_aborts_before_deleting(
        self,
    ) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)
        client.entries = [{"id": "booked-in-the-meantime"}]

        with self._with_client(client):
            result = ledger.delete_empty_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(client.delete_calls, [])
        self.assertEqual(result["status"], "precondition_failed")
        self.assertIn("evidence changed after preview", result["error"])

    def test_a_stale_identity_aborts_before_deleting(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)
        client.record["account_id"] = "48999"

        with self._with_client(client):
            result = ledger.delete_empty_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(client.delete_calls, [])
        self.assertEqual(result["status"], "precondition_failed")

    def test_the_approval_is_single_use(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)

        with self._with_client(client):
            ledger.delete_empty_ledger_account_from_approval(prepared["approval_id"])
            with self.assertRaises(MoneybirdError):
                ledger.delete_empty_ledger_account_from_approval(
                    prepared["approval_id"]
                )

        self.assertEqual(client.delete_calls, [LEDGER_ID])

    def test_read_only_mode_refuses_the_destructive_action(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)

        with mock.patch.dict(
            os.environ, {"MONEYBIRD_CAPABILITY_MODE": "read_only"}
        ):
            with self._with_client(client):
                with self.assertRaisesRegex(MoneybirdError, "writes are disabled"):
                    ledger.delete_empty_ledger_account_from_approval(
                        prepared["approval_id"]
                    )

        self.assertEqual(client.delete_calls, [])

    def test_an_approval_cannot_delete_in_another_administration(self) -> None:
        client = _LedgerAccountClient()
        prepared = self._prepare(client)
        other = _LedgerAccountClient(administration_id=OTHER_ADMINISTRATION)

        with self._with_client(other):
            with self.assertRaises(MoneybirdError):
                ledger.delete_empty_ledger_account_from_approval(
                    prepared["approval_id"]
                )

        self.assertEqual(other.delete_calls, [])
        self.assertEqual(client.delete_calls, [])

    def test_an_account_older_than_the_evidence_window_is_refused(self) -> None:
        client = _LedgerAccountClient()
        old = date.today().replace(day=1) - timedelta(days=500)
        client.created_at = old.isoformat() + "T09:00:00.000Z"
        client.record["created_at"] = client.created_at
        with self.assertRaisesRegex(MoneybirdError, "12 months"):
            self._prepare(client)
        self.assertEqual(client.delete_calls, [])

    def test_a_ledger_without_a_creation_date_is_refused(self) -> None:
        client = _LedgerAccountClient()
        client.record["created_at"] = ""
        with self.assertRaises(MoneybirdError):
            self._prepare(client, expected_created_date="")
        self.assertEqual(client.delete_calls, [])


if __name__ == "__main__":
    unittest.main()
