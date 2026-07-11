"""Tests for the Moneybird OAuth flow and the credential fallback chain."""
from __future__ import annotations

import os
import tempfile
import time
import unittest
import urllib.parse
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird import credentials, oauth
from moneybird.config import MoneybirdError

FAKE_APP = {
    "MONEYBIRD_OAUTH_CLIENT_ID": "client-id-123",
    "MONEYBIRD_OAUTH_CLIENT_SECRET": "client-secret-456",
}


class AuthorizeUrlTests(unittest.TestCase):
    def test_authorize_url_contains_registered_client_and_scopes(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            url = oauth.build_authorize_url(state="xyz")
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(parsed.netloc, "moneybird.com")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["client_id"], "client-id-123")
        self.assertEqual(params["redirect_uri"], oauth.OOB_REDIRECT_URI)
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["scope"], oauth.DEFAULT_OAUTH_SCOPES)
        self.assertEqual(params["state"], "xyz")

    def test_missing_client_config_raises_with_registration_hint(self) -> None:
        cleared = {key: "" for key in FAKE_APP}
        with mock.patch.dict(os.environ, cleared):
            with self.assertRaises(MoneybirdError) as ctx:
                oauth.build_authorize_url()
        self.assertIn("moneybird.com/user/applications/new", str(ctx.exception))


class TokenRequestTests(unittest.TestCase):
    def test_exchange_sends_authorization_code_grant(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            with mock.patch.object(
                oauth, "_token_request", return_value={"access_token": "at"}
            ) as request:
                tokens = oauth.exchange_authorization_code(" the-code \n")
        form = request.call_args.args[0]
        self.assertEqual(form["grant_type"], "authorization_code")
        self.assertEqual(form["code"], "the-code")
        self.assertEqual(form["redirect_uri"], oauth.OOB_REDIRECT_URI)
        self.assertEqual(form["client_secret"], "client-secret-456")
        self.assertEqual(tokens["access_token"], "at")

    def test_refresh_sends_refresh_token_grant(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            with mock.patch.object(
                oauth, "_token_request", return_value={"access_token": "at2"}
            ) as request:
                oauth.refresh_access_token("rt")
        form = request.call_args.args[0]
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "rt")


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._data_dir = tempfile.mkdtemp(prefix="moneybird_oauth_test_")
        patcher = mock.patch.dict(
            os.environ, {"MONEYBIRD_MCP_DATA_DIR": self._data_dir, **FAKE_APP}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_stored_tokens_returns_none(self) -> None:
        self.assertIsNone(oauth.get_access_token())

    def test_non_expiring_token_round_trips(self) -> None:
        oauth.store_tokens({"access_token": "at", "refresh_token": "rt", "scope": "bank"})
        self.assertEqual(oauth.get_access_token(), "at")
        self.assertEqual(oauth.load_tokens()["scope"], "bank")

    def test_profiles_are_isolated(self) -> None:
        oauth.store_tokens({"access_token": "at-default"})
        oauth.store_tokens({"access_token": "at-other"}, profile="other")
        self.assertEqual(oauth.get_access_token(), "at-default")
        self.assertEqual(oauth.get_access_token("other"), "at-other")

    def test_expired_token_is_refreshed_and_persisted(self) -> None:
        oauth.store_tokens(
            {
                "access_token": "stale",
                "refresh_token": "rt-old",
                "expires_in": 7200,
                "obtained_at": int(time.time()) - 8000,
            }
        )
        fresh = {"access_token": "fresh", "refresh_token": "rt-new", "expires_in": 7200}
        with mock.patch.object(
            oauth, "refresh_access_token", return_value=fresh
        ) as refresh:
            self.assertEqual(oauth.get_access_token(), "fresh")
        refresh.assert_called_once_with("rt-old")
        stored = oauth.load_tokens()
        self.assertEqual(stored["access_token"], "fresh")
        self.assertEqual(stored["refresh_token"], "rt-new")

    def test_expired_token_without_refresh_token_raises(self) -> None:
        oauth.store_tokens(
            {
                "access_token": "stale",
                "expires_in": 60,
                "obtained_at": int(time.time()) - 3600,
            }
        )
        with self.assertRaises(MoneybirdError):
            oauth.get_access_token()


class CredentialFallbackTests(unittest.TestCase):
    def test_oauth_store_is_used_when_env_token_is_absent(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MONEYBIRD_ACCESS_TOKEN": "", "MONEYBIRD_ADMINISTRATION_ID": "123"},
        ):
            with mock.patch.object(oauth, "get_access_token", return_value="oauth-at"):
                resolved = credentials.resolve_credentials()
        self.assertEqual(resolved.source, "oauth")
        self.assertEqual(resolved.token, "oauth-at")
        self.assertEqual(resolved.administration_id, "123")

    def test_env_token_still_wins_over_oauth_store(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "env-at"}):
            with mock.patch.object(oauth, "get_access_token", return_value="oauth-at"):
                resolved = credentials.resolve_credentials()
        self.assertEqual(resolved.source, "environment")
        self.assertEqual(resolved.token, "env-at")

    def test_no_credentials_anywhere_raises_with_oauth_hint(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": ""}):
            with mock.patch.object(oauth, "get_access_token", return_value=None):
                with self.assertRaises(MoneybirdError) as ctx:
                    credentials.resolve_credentials()
        self.assertIn("oauth_login", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
