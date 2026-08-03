from __future__ import annotations

import os
import unittest
from unittest import mock

from moneybird_mcp import sync
from moneybird_mcp.client import MoneybirdClient
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import CREDENTIAL_MODE_ENV
from moneybird_mcp.tools import core


class ClientAdministrationAccessTests(unittest.TestCase):
    def test_selected_administration_must_be_in_live_membership(self) -> None:
        client = MoneybirdClient("unrelated-token", "123")
        with mock.patch.object(
            client,
            "list_administrations",
            return_value=[{"id": "456", "name": "Other"}],
        ):
            with self.assertRaisesRegex(MoneybirdError, "does not currently have access"):
                client.require_current_administration_access()

    def test_matching_live_membership_is_returned(self) -> None:
        client = MoneybirdClient("active-token", "123")
        expected = {"id": "123", "name": "Allowed"}
        with mock.patch.object(
            client,
            "list_administrations",
            return_value=[expected],
        ):
            self.assertIs(client.require_current_administration_access(), expected)


class SearchCacheAuthorizationTests(unittest.TestCase):
    class _DeniedClient:
        administration_id = "123"

        def require_current_administration_access(self):
            raise MoneybirdError("membership revoked")

    def test_search_authorizes_before_reading_sync_or_fts_state(self) -> None:
        with (
            mock.patch.object(core.ctx, "get_client", return_value=self._DeniedClient()),
            mock.patch.object(core, "load_sync_index") as load_index,
            mock.patch.object(core, "search_fts") as fts,
        ):
            with self.assertRaisesRegex(MoneybirdError, "membership revoked"):
                core.search("victim customer")
        load_index.assert_not_called()
        fts.assert_not_called()

    class _LiveOnlyClient:
        administration_id = "123"

        def require_current_administration_access(self):
            return {"id": "123"}

        def __getattr__(self, name):
            if name.startswith("list_"):
                return lambda *args, **kwargs: []
            raise AttributeError(name)

    def test_hosted_search_never_opens_durable_json_or_fts_cache(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {CREDENTIAL_MODE_ENV: "hosted_request_only"},
                clear=False,
            ),
            mock.patch.object(
                core.ctx,
                "get_client",
                return_value=self._LiveOnlyClient(),
            ),
            mock.patch.object(core, "load_sync_index") as load_index,
            mock.patch.object(core, "refresh_fts_index") as refresh_fts,
            mock.patch.object(core, "search_fts") as search_fts,
        ):
            result = core.search("victim customer")
        self.assertEqual(result["source"], "live_fallback")
        load_index.assert_not_called()
        refresh_fts.assert_not_called()
        search_fts.assert_not_called()

    def test_hosted_sync_is_rejected_before_cache_access(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {CREDENTIAL_MODE_ENV: "hosted_request_only"},
                clear=False,
            ),
            mock.patch.object(sync, "load_sync_index") as load_index,
        ):
            with self.assertRaisesRegex(MoneybirdError, "Durable search caches"):
                sync.sync_search_index_data(self._LiveOnlyClient())
        load_index.assert_not_called()
