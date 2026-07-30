"""Tests for the Moneybird OAuth flow and the credential fallback chain."""
from __future__ import annotations

import io
import json
import os
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
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


class AuthorizationCallbackTests(unittest.TestCase):
    def test_code_is_extracted_when_state_matches(self) -> None:
        state = oauth.generate_state()
        url = f"https://gateway.example/callback?code=abc123&state={state}"
        self.assertEqual(
            oauth.parse_authorization_callback(url, expected_state=state), "abc123"
        )

    def test_state_mismatch_raises_without_leaking_the_code(self) -> None:
        url = "https://gateway.example/callback?code=abc123&state=forged"
        with self.assertRaises(MoneybirdError) as caught:
            oauth.parse_authorization_callback(url, expected_state="expected")
        self.assertIn("state mismatch", str(caught.exception))
        self.assertNotIn("abc123", str(caught.exception))

    def test_provider_error_is_reported(self) -> None:
        url = (
            "https://gateway.example/callback?error=access_denied"
            "&error_description=The+user+denied+access"
        )
        with self.assertRaises(MoneybirdError) as caught:
            oauth.parse_authorization_callback(url)
        self.assertIn("access_denied", str(caught.exception))

    def test_missing_code_raises(self) -> None:
        with self.assertRaises(MoneybirdError):
            oauth.parse_authorization_callback("https://gateway.example/callback?state=x")

    def test_generate_state_is_random_and_urlsafe(self) -> None:
        first, second = oauth.generate_state(), oauth.generate_state()
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 32)
        self.assertEqual(first, urllib.parse.quote(first, safe="-_"))


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

    def test_token_endpoint_error_never_exposes_response_credentials(self) -> None:
        upstream = urllib.error.HTTPError(
            oauth.OAUTH_TOKEN_URL,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"refresh_token":"do-not-render"}'),
        )
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=upstream,
        ):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "authorization_code"})
        message = str(caught.exception)
        self.assertIn("HTTP 400", message)
        self.assertNotIn("do-not-render", message)

    def test_token_response_requires_nonblank_string_access_token(self) -> None:
        invalid_payloads = [
            None,
            [],
            "access_token",
            {"access_token": ""},
            {"access_token": " \n"},
            {"access_token": 123},
        ]
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                response = mock.MagicMock()
                response.__enter__.return_value = response
                response.read.return_value = json.dumps(payload).encode("utf-8")
                with mock.patch.object(
                    urllib.request,
                    "urlopen",
                    return_value=response,
                ):
                    with self.assertRaisesRegex(
                        MoneybirdError,
                        "valid access token",
                    ):
                        oauth._token_request({"grant_type": "authorization_code"})


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
