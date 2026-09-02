"""Every ledger-account write drops the cached reference data it just invalidated.

``list_ledger_accounts`` is cached for ten minutes because nearly every read and
every ``prepare_*`` resolves it. That makes the cache a correctness surface for
the three writes that change what it holds: after creating, renaming or removing
an account, the very next preview must not resolve names and ids from a snapshot
taken before the write.

The tests drive the real ``MoneybirdClient`` and the real cache, with only the
HTTP layer replaced, so "the cache was invalidated" is proved the way a caller
would notice it -- the next lookup goes back to the provider and returns the new
value -- rather than by asserting that a helper was called.

Every identifier and name below is synthesized.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from moneybird_mcp import reference_cache, safety
from moneybird_mcp.client import MoneybirdClient
from moneybird_mcp.config import MoneybirdError, MoneybirdHTTPError
from moneybird_mcp.credentials import set_active_administration_id
from moneybird_mcp.tools import ledger

ADMINISTRATION = "9001"
LEDGER_ID = "44001"
OLD_NAME = "Sandbox Verrekenrekening"
NEW_NAME = "Sandbox Tussenrekening"
OLD_RGS = "WBedOvbOvb"
NEW_RGS = "WBedOvbAlg"


class _FakeProvider:
    """Stands in for the HTTP layer, holding one mutable ledger account."""

    def __init__(self) -> None:
        self.record = {
            "id": LEDGER_ID,
            "name": OLD_NAME,
            "account_type": "expenses",
            "account_id": "48800",
            "parent_id": None,
            "active": True,
            "created_at": "2026-01-05T09:00:00.000Z",
            "updated_at": "2026-01-05T09:00:00.000Z",
            "taxonomy_item": {"code": OLD_RGS},
        }
        self.list_requests = 0
        self.patch_requests = 0
        self.post_requests = 0
        self.delete_requests = 0
        self.deleted = False

    def __call__(self, method, path, *args, **kwargs):
        collection = f"/{ADMINISTRATION}/ledger_accounts.json"
        record = f"/{ADMINISTRATION}/ledger_accounts/{LEDGER_ID}.json"
        if method == "GET" and path == collection:
            self.list_requests += 1
            return [] if self.deleted else [dict(self.record)]
        if method == "GET" and path == record:
            if self.deleted:
                # The delete executor reads 404 as proof of physical removal.
                raise MoneybirdHTTPError("not found", status_code=404)
            return dict(self.record)
        if method == "PATCH" and path == record:
            self.patch_requests += 1
            body = kwargs.get("body") or {}
            self.record["name"] = (body.get("ledger_account") or {}).get(
                "name", self.record["name"]
            )
            if body.get("rgs_code"):
                self.record["taxonomy_item"] = {"code": body["rgs_code"]}
            self.record["updated_at"] = "2026-02-02T10:00:00.000Z"
            return dict(self.record)
        if method == "POST" and path == collection:
            self.post_requests += 1
            return dict(self.record)
        if method == "DELETE" and path == record:
            self.delete_requests += 1
            self.deleted = True
            return None
        # The empty-ledger preflight reads assets and the journal-entries report;
        # neither is cached, and an empty answer is what makes the account eligible.
        if method == "GET" and path == f"/{ADMINISTRATION}/assets.json":
            return []
        if method == "GET" and "reports/journal_entries" in path:
            return []
        raise AssertionError(f"unexpected request: {method} {path}")


class LedgerWriteCacheInvalidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(prefix="moneybird_ledger_cache_")
        self._env = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name,
                "MONEYBIRD_CAPABILITY_MODE": "write_enabled",
                "MONEYBIRD_CREDENTIAL_MODE": "local",
            },
        )
        self._env.start()
        reference_cache.clear()
        set_active_administration_id(ADMINISTRATION)
        safety.clear_pending_approvals()

        self.provider = _FakeProvider()
        self.client = MoneybirdClient(
            token="synthetic-token", administration_id=ADMINISTRATION
        )
        self._request = mock.patch.object(
            self.client, "_request", side_effect=self.provider
        )
        self._request.start()

    def tearDown(self) -> None:
        self._request.stop()
        safety.clear_pending_approvals()
        set_active_administration_id(None)
        reference_cache.clear()
        self._env.stop()
        self._temp_dir.cleanup()

    def _with_client(self):
        return mock.patch.object(ledger.ctx, "get_client", return_value=self.client)

    def _warm_the_cache(self) -> None:
        """Read the collection twice and prove the second read never left the process."""
        self.assertEqual(
            self.client.list_ledger_accounts()[0]["name"], OLD_NAME
        )
        self.assertEqual(
            self.client.list_ledger_accounts()[0]["name"], OLD_NAME
        )
        self.assertEqual(self.provider.list_requests, 1)

    # -- the defect this file exists for ------------------------------------
    def test_a_verified_rename_makes_the_next_lookup_return_the_new_name(self) -> None:
        self._warm_the_cache()

        with self._with_client():
            prepared = ledger.prepare_update_ledger_account(
                ledger_account_id=LEDGER_ID, rgs_code=NEW_RGS, name=NEW_NAME
            )
            result = ledger.update_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "updated")
        self.assertTrue(result["verification"]["fully_verified"])
        self.assertEqual(self.provider.patch_requests, 1)

        # The cache must no longer answer for this administration.
        refreshed = self.client.list_ledger_accounts()
        self.assertEqual(self.provider.list_requests, 2)
        self.assertEqual(refreshed[0]["name"], NEW_NAME)
        self.assertEqual(refreshed[0]["taxonomy_item"]["code"], NEW_RGS)

    def test_a_taxonomy_only_update_also_invalidates(self) -> None:
        self._warm_the_cache()

        with self._with_client():
            prepared = ledger.prepare_update_ledger_account(
                ledger_account_id=LEDGER_ID, rgs_code=NEW_RGS
            )
            result = ledger.update_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "updated")
        refreshed = self.client.list_ledger_accounts()
        self.assertEqual(self.provider.list_requests, 2)
        self.assertEqual(refreshed[0]["taxonomy_item"]["code"], NEW_RGS)

    def test_only_the_written_administration_is_dropped(self) -> None:
        self._warm_the_cache()
        other = reference_cache.cached_read(
            token="synthetic-token",
            administration_id="9002",
            resource="ledger_accounts",
            ttl_seconds=600.0,
            loader=lambda: [{"id": "55001", "name": "Other administration"}],
        )
        self.assertEqual(other[0]["name"], "Other administration")

        with self._with_client():
            prepared = ledger.prepare_update_ledger_account(
                ledger_account_id=LEDGER_ID, rgs_code=NEW_RGS, name=NEW_NAME
            )
            ledger.update_ledger_account_from_approval(prepared["approval_id"])

        untouched_loads = 0

        def loader():
            nonlocal untouched_loads
            untouched_loads += 1
            return [{"id": "55001", "name": "Other administration"}]

        reference_cache.cached_read(
            token="synthetic-token",
            administration_id="9002",
            resource="ledger_accounts",
            ttl_seconds=600.0,
            loader=loader,
        )
        self.assertEqual(untouched_loads, 0)

    # -- a write that did not happen must not look like one ------------------
    def test_a_stale_preview_neither_patches_nor_drops_the_cache(self) -> None:
        with self._with_client():
            prepared = ledger.prepare_update_ledger_account(
                ledger_account_id=LEDGER_ID, rgs_code=NEW_RGS, name=NEW_NAME
            )

        # Somebody else edits the account between preview and approval.
        self.provider.record["account_id"] = "48999"
        self._warm_the_cache()

        with self._with_client():
            result = ledger.update_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(result["status"], "precondition_failed")
        self.assertEqual(self.provider.patch_requests, 0)
        self.client.list_ledger_accounts()
        self.assertEqual(
            self.provider.list_requests,
            1,
            "an aborted write must not invalidate: nothing changed upstream",
        )

    def test_a_refused_write_leaves_the_cache_alone(self) -> None:
        with self._with_client():
            prepared = ledger.prepare_update_ledger_account(
                ledger_account_id=LEDGER_ID, rgs_code=NEW_RGS, name=NEW_NAME
            )
        self._warm_the_cache()

        with mock.patch.dict(os.environ, {"MONEYBIRD_CAPABILITY_MODE": "read_only"}):
            with self._with_client():
                with self.assertRaisesRegex(MoneybirdError, "writes are disabled"):
                    ledger.update_ledger_account_from_approval(
                        prepared["approval_id"]
                    )

        self.assertEqual(self.provider.patch_requests, 0)
        self.client.list_ledger_accounts()
        self.assertEqual(self.provider.list_requests, 1)

    def test_a_write_that_landed_but_failed_to_verify_still_invalidates(self) -> None:
        """The case that argues against invalidating only on success.

        The PATCH was accepted, so the cached list is wrong. Verification failing
        makes the next read more important, not less.
        """
        self._warm_the_cache()

        with self._with_client():
            prepared = ledger.prepare_update_ledger_account(
                ledger_account_id=LEDGER_ID, rgs_code=NEW_RGS, name=NEW_NAME
            )

        original = self.provider.__call__

        def divergent(method, path, *args, **kwargs):
            result = original(method, path, *args, **kwargs)
            if method == "PATCH":
                # Provider applied something other than what was asked for.
                self.provider.record["name"] = "Renamed by the provider"
            return result

        with self._with_client():
            with mock.patch.object(
                self.client, "_request", side_effect=divergent
            ):
                result = ledger.update_ledger_account_from_approval(
                    prepared["approval_id"]
                )

        self.assertEqual(result["status"], "verification_failed")
        self.assertFalse(result["verification"]["fully_verified"])
        refreshed = self.client.list_ledger_accounts()
        self.assertEqual(self.provider.list_requests, 2)
        self.assertEqual(refreshed[0]["name"], "Renamed by the provider")

    # -- the same invariant for the two writes that already had it -----------
    def test_creating_an_account_invalidates(self) -> None:
        self._warm_the_cache()

        with self._with_client():
            prepared = ledger.prepare_create_ledger_account(
                name="Sandbox Nieuwe Rekening",
                account_type="expenses",
                rgs_code=NEW_RGS,
            )
            ledger.create_ledger_account_from_approval(prepared["approval_id"])

        self.client.list_ledger_accounts()
        self.assertEqual(self.provider.list_requests, 2)

    def test_deleting_an_account_invalidates(self) -> None:
        self._warm_the_cache()

        with self._with_client():
            prepared = ledger.prepare_delete_empty_ledger_account(
                ledger_account_id=LEDGER_ID,
                expected_name=OLD_NAME,
                expected_created_date=self.provider.record["created_at"][:10],
                test_provenance="Synthesized cache-invalidation regression",
            )
            ledger.delete_empty_ledger_account_from_approval(
                prepared["approval_id"]
            )

        self.assertEqual(self.provider.delete_requests, 1)
        self.assertEqual(self.client.list_ledger_accounts(), [])
        self.assertEqual(self.provider.list_requests, 2)


if __name__ == "__main__":
    unittest.main()
