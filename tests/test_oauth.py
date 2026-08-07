"""Tests for the Moneybird OAuth flow, credential storage, and the fallback chain.

Every Moneybird interaction here is mocked. No test needs a real client id,
client secret, account, or network access; `NoAccidentalNetworkTests` pins that
for the pure paths.
"""
from __future__ import annotations

import io
import json
import os
import stat
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird_mcp import credentials, oauth, oauth_scopes, oauth_store
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.oauth_store import FileTokenStore, OAuthConnection

# Obvious placeholders. Nothing in this suite is a real credential.
FAKE_APP = {
    "MONEYBIRD_OAUTH_CLIENT_ID": "client-id-123",
    "MONEYBIRD_OAUTH_CLIENT_SECRET": "client-secret-456",
}


class _StoreCase(unittest.TestCase):
    """Redirect the credential store at a fresh temporary directory."""

    def setUp(self) -> None:
        self._data_dir = tempfile.mkdtemp(prefix="moneybird_oauth_test_")
        patcher = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._data_dir,
                oauth_scopes.SCOPES_ENV: "",
                **FAKE_APP,
            },
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        store_patcher = mock.patch.object(
            oauth_store, "_store", FileTokenStore()
        )
        store_patcher.start()
        self.addCleanup(store_patcher.stop)


class AuthorizeUrlTests(unittest.TestCase):
    def test_authorize_url_contains_registered_client_and_scopes(self) -> None:
        with mock.patch.dict(os.environ, {**FAKE_APP, oauth_scopes.SCOPES_ENV: ""}):
            url = oauth.build_authorize_url(state="xyz")
        parsed = urllib.parse.urlparse(url)
        params = dict(urllib.parse.parse_qsl(parsed.query))
        self.assertEqual(parsed.scheme, "https")
        self.assertEqual(parsed.netloc, "moneybird.com")
        self.assertEqual(parsed.path, "/oauth/authorize")
        self.assertEqual(params["client_id"], "client-id-123")
        self.assertEqual(params["redirect_uri"], oauth.OOB_REDIRECT_URI)
        self.assertEqual(params["response_type"], "code")
        self.assertEqual(params["scope"], oauth.DEFAULT_OAUTH_SCOPES)
        self.assertEqual(params["state"], "xyz")

    def test_oob_redirect_uri_is_percent_encoded_in_the_query(self) -> None:
        """The urn contains colons; an unencoded redirect_uri is rejected upstream."""
        with mock.patch.dict(os.environ, FAKE_APP):
            url = oauth.build_authorize_url()
        self.assertIn("redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob", url)
        self.assertNotIn("redirect_uri=urn:ietf", url)

    def test_scopes_are_space_separated_and_encoded_as_plus(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            url = oauth.build_authorize_url(scope="bank documents")
        self.assertIn("scope=bank+documents", url)
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(params["scope"], "bank documents")

    def test_state_is_omitted_when_not_supplied(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            url = oauth.build_authorize_url()
        self.assertNotIn("state=", url)

    def test_environment_scope_override_is_honoured(self) -> None:
        with mock.patch.dict(
            os.environ, {**FAKE_APP, oauth_scopes.SCOPES_ENV: "invoicing"}
        ):
            url = oauth.build_authorize_url()
        params = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(url).query))
        self.assertEqual(params["scope"], "sales_invoices settings")

    def test_missing_client_config_raises_with_registration_hint(self) -> None:
        cleared = {key: "" for key in FAKE_APP}
        with mock.patch.dict(os.environ, cleared):
            with self.assertRaises(MoneybirdError) as ctx:
                oauth.build_authorize_url()
        self.assertIn("moneybird.com/user/applications/new", str(ctx.exception))

    def test_missing_client_config_names_only_the_absent_variable(self) -> None:
        with mock.patch.dict(
            os.environ, {**FAKE_APP, "MONEYBIRD_OAUTH_CLIENT_SECRET": ""}
        ):
            with self.assertRaises(MoneybirdError) as ctx:
                oauth.oauth_client_config()
        message = str(ctx.exception)
        self.assertIn("MONEYBIRD_OAUTH_CLIENT_SECRET", message)
        self.assertNotIn("MONEYBIRD_OAUTH_CLIENT_ID is not set", message)
        # Never echo the value of the one that *was* supplied.
        self.assertNotIn("client-id-123", message)


class ScopeTests(unittest.TestCase):
    def test_full_profile_is_every_documented_scope(self) -> None:
        self.assertEqual(
            oauth_scopes.scopes_for_profile("full"), oauth_scopes.KNOWN_SCOPES
        )
        self.assertEqual(
            set(oauth_scopes.KNOWN_SCOPES),
            {
                "sales_invoices",
                "documents",
                "estimates",
                "bank",
                "time_entries",
                "settings",
            },
        )

    def test_every_capability_maps_to_a_documented_scope(self) -> None:
        for entry in oauth_scopes.CAPABILITY_SCOPES:
            with self.subTest(area=entry.area):
                self.assertIn(entry.scope, oauth_scopes.KNOWN_SCOPES)
                self.assertTrue(entry.reason)
                self.assertTrue(entry.examples)

    def test_capability_map_covers_every_scope_the_full_profile_requests(self) -> None:
        """A scope nobody can justify must not be in the default request."""
        justified = {entry.scope for entry in oauth_scopes.CAPABILITY_SCOPES}
        self.assertEqual(justified, set(oauth_scopes.scopes_for_profile("full")))

    def test_narrow_profiles_are_subsets_of_full(self) -> None:
        for name, scopes in oauth_scopes.SCOPE_PROFILES.items():
            with self.subTest(profile=name):
                self.assertTrue(set(scopes) <= set(oauth_scopes.KNOWN_SCOPES))

    def test_explicit_scope_list_is_normalised_and_deduplicated(self) -> None:
        self.assertEqual(
            oauth_scopes.parse_scopes("settings, bank settings"),
            ("bank", "settings"),
        )

    def test_unknown_scope_is_rejected_before_the_browser_opens(self) -> None:
        with self.assertRaises(MoneybirdError) as ctx:
            oauth_scopes.parse_scopes("bank sales_invoice")
        self.assertIn("sales_invoice", str(ctx.exception))

    def test_unknown_profile_is_rejected(self) -> None:
        with self.assertRaises(MoneybirdError):
            oauth_scopes.scopes_for_profile("everything")

    def test_blank_falls_back_to_the_default_profile(self) -> None:
        self.assertEqual(
            oauth_scopes.parse_scopes("  "),
            oauth_scopes.scopes_for_profile(oauth_scopes.DEFAULT_SCOPE_PROFILE),
        )

    def test_missing_scopes_reports_what_moneybird_withheld(self) -> None:
        granted = "sales_invoices settings"
        self.assertEqual(
            oauth_scopes.missing_scopes(granted, ("sales_invoices", "bank", "documents")),
            ("bank", "documents"),
        )
        self.assertEqual(oauth_scopes.missing_scopes(granted, ("settings",)), ())
        # An unreported scope string cannot prove anything was granted.
        self.assertEqual(oauth_scopes.missing_scopes("", ("bank",)), ("bank",))


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
        self.assertEqual(form["client_id"], "client-id-123")
        self.assertEqual(form["client_secret"], "client-secret-456")
        self.assertEqual(tokens["access_token"], "at")

    def test_exchange_uses_the_redirect_uri_it_was_given(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            with mock.patch.object(
                oauth, "_token_request", return_value={"access_token": "at"}
            ) as request:
                oauth.exchange_authorization_code(
                    "code", redirect_uri="https://app.example/oauth/callback"
                )
        self.assertEqual(
            request.call_args.args[0]["redirect_uri"],
            "https://app.example/oauth/callback",
        )

    def test_a_pasted_url_is_rejected_before_it_reaches_moneybird(self) -> None:
        """`invalid_grant` for a pasted URL sends the user hunting the wrong bug."""
        with mock.patch.dict(os.environ, FAKE_APP):
            with mock.patch.object(oauth, "_token_request") as request:
                for pasted in (
                    "https://moneybird.com/oauth/authorize?code=abc",
                    "code: abc123",
                    "",
                    "   ",
                ):
                    with self.subTest(pasted=pasted):
                        with self.assertRaises(MoneybirdError):
                            oauth.exchange_authorization_code(pasted)
        request.assert_not_called()

    def test_refresh_sends_refresh_token_grant(self) -> None:
        with mock.patch.dict(os.environ, FAKE_APP):
            with mock.patch.object(
                oauth, "_token_request", return_value={"access_token": "at2"}
            ) as request:
                oauth.refresh_access_token("rt")
        form = request.call_args.args[0]
        self.assertEqual(form["grant_type"], "refresh_token")
        self.assertEqual(form["refresh_token"], "rt")
        self.assertNotIn("code", form)

    def test_token_endpoint_posts_form_encoded_to_the_official_endpoint(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"access_token":"at"}'
        with mock.patch.object(
            urllib.request, "urlopen", return_value=response
        ) as urlopen:
            oauth._token_request({"grant_type": "refresh_token"})
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://moneybird.com/oauth/token")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.get_header("Content-type"), "application/x-www-form-urlencoded"
        )
        self.assertEqual(urlopen.call_args.kwargs["timeout"], 20)

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

    def test_invalid_client_error_points_at_the_application_credentials(self) -> None:
        upstream = urllib.error.HTTPError(
            oauth.OAUTH_TOKEN_URL,
            401,
            "Unauthorized",
            {},
            io.BytesIO(
                b'{"error":"invalid_client",'
                b'"error_description":"Client authentication failed"}'
            ),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=upstream):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "authorization_code"})
        message = str(caught.exception)
        self.assertIn("invalid_client", message)
        self.assertIn("MONEYBIRD_OAUTH_CLIENT_SECRET", message)

    def test_invalid_grant_error_explains_a_used_or_expired_code(self) -> None:
        upstream = urllib.error.HTTPError(
            oauth.OAUTH_TOKEN_URL,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"invalid_grant"}'),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=upstream):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "authorization_code"})
        message = str(caught.exception)
        self.assertIn("single-use", message)

    def test_server_error_is_reported_as_moneybirds_problem(self) -> None:
        upstream = urllib.error.HTTPError(
            oauth.OAUTH_TOKEN_URL, 503, "Service Unavailable", {}, io.BytesIO(b"")
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=upstream):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("HTTP 503", str(caught.exception))
        self.assertIn("Moneybird's side", str(caught.exception))

    def test_timeout_says_nothing_was_retried(self) -> None:
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError()
        ):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "authorization_code"})
        message = str(caught.exception)
        self.assertIn("did not respond", message)
        self.assertIn("single-use", message)

    def test_network_failure_is_reported_without_a_traceback_chain(self) -> None:
        with mock.patch.object(
            urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError(ConnectionRefusedError()),
        ):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("Could not reach", str(caught.exception))
        self.assertIsNone(caught.exception.__cause__)

    def test_a_single_grant_is_never_retried(self) -> None:
        """Repeating a consumed authorization code turns a blip into a dead grant."""
        with mock.patch.object(
            urllib.request, "urlopen", side_effect=TimeoutError()
        ) as urlopen:
            with self.assertRaises(MoneybirdError):
                oauth._token_request({"grant_type": "authorization_code"})
        self.assertEqual(urlopen.call_count, 1)

    def test_non_json_response_is_reported_as_such(self) -> None:
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"<html>maintenance</html>"
        with mock.patch.object(urllib.request, "urlopen", return_value=response):
            with self.assertRaises(MoneybirdError) as caught:
                oauth._token_request({"grant_type": "refresh_token"})
        self.assertIn("not JSON", str(caught.exception))

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


class ConnectionModelTests(unittest.TestCase):
    def test_token_response_is_parsed_into_a_connection(self) -> None:
        connection = OAuthConnection.from_token_response(
            {
                "access_token": " at ",
                "refresh_token": "rt",
                "token_type": "bearer",
                "scope": "bank settings",
                "expires_in": 7200,
                "created_at": 1000,
                "unexpected_future_field": "keep-me",
            },
            obtained_at=2000,
        )
        self.assertEqual(connection.access_token, "at")
        self.assertEqual(connection.refresh_token, "rt")
        self.assertEqual(connection.scope, "bank settings")
        self.assertEqual(connection.expires_in, 7200)
        self.assertEqual(connection.created_at, 1000)
        self.assertEqual(connection.obtained_at, 2000)
        self.assertEqual(connection.expires_at, 9200)
        # Unknown fields survive a round trip instead of being dropped.
        self.assertEqual(connection.extra["unexpected_future_field"], "keep-me")
        self.assertEqual(connection.to_record()["unexpected_future_field"], "keep-me")

    def test_a_token_without_expiry_never_expires(self) -> None:
        connection = OAuthConnection(access_token="at", obtained_at=0)
        self.assertIsNone(connection.expires_at)
        self.assertFalse(connection.is_expired(margin_seconds=10_000))

    def test_expiry_margin_triggers_before_the_deadline(self) -> None:
        connection = OAuthConnection(
            access_token="at", expires_in=100, obtained_at=1000
        )
        self.assertFalse(connection.is_expired(margin_seconds=60, now=1000))
        self.assertTrue(connection.is_expired(margin_seconds=60, now=1041))
        self.assertTrue(connection.is_expired(now=1100))

    def test_a_connection_requires_an_access_token(self) -> None:
        with self.assertRaises(MoneybirdError):
            OAuthConnection(access_token="")

    def test_refresh_merge_keeps_a_refresh_token_moneybird_omitted(self) -> None:
        """A refresh answer without a refresh_token means unchanged, not cleared."""
        stored = OAuthConnection(
            access_token="old",
            refresh_token="rt-keep",
            scope="bank settings",
            expires_in=7200,
            obtained_at=1000,
            administration_id="123",
        )
        merged = stored.merged_with_refresh({"access_token": "new"})
        self.assertEqual(merged.access_token, "new")
        self.assertEqual(merged.refresh_token, "rt-keep")
        self.assertEqual(merged.scope, "bank settings")
        self.assertEqual(merged.expires_in, 7200)
        # The selected administration is local state, not part of the grant.
        self.assertEqual(merged.administration_id, "123")

    def test_refresh_merge_accepts_a_rotated_refresh_token(self) -> None:
        stored = OAuthConnection(access_token="old", refresh_token="rt-old")
        merged = stored.merged_with_refresh(
            {"access_token": "new", "refresh_token": "rt-new", "expires_in": 60}
        )
        self.assertEqual(merged.refresh_token, "rt-new")
        self.assertEqual(merged.expires_in, 60)


class RedactionTests(unittest.TestCase):
    SECRETS = {
        "access_token": "AT-SECRET-VALUE",
        "refresh_token": "RT-SECRET-VALUE",
    }

    def _connection(self) -> OAuthConnection:
        return OAuthConnection(
            access_token=self.SECRETS["access_token"],
            refresh_token=self.SECRETS["refresh_token"],
            scope="bank",
            administration_id="123",
        )

    def _assert_clean(self, text: str) -> None:
        for value in self.SECRETS.values():
            self.assertNotIn(value, text)

    def test_repr_and_str_redact_both_tokens(self) -> None:
        connection = self._connection()
        self._assert_clean(repr(connection))
        self._assert_clean(str(connection))
        self._assert_clean(f"{connection}")
        self._assert_clean(f"{connection!r}")
        self._assert_clean("{}".format(connection))  # noqa: UP032 - explicit path
        self.assertIn("redacted", repr(connection))

    def test_repr_survives_being_embedded_in_a_container(self) -> None:
        # A dict or list repr calls repr() on its members, which is how a token
        # most often reaches a traceback frame.
        self._assert_clean(repr({"connection": self._connection()}))
        self._assert_clean(repr([self._connection()]))

    def test_describe_reports_presence_not_content(self) -> None:
        summary = self._connection().describe()
        self._assert_clean(json.dumps(summary))
        self.assertTrue(summary["has_access_token"])
        self.assertTrue(summary["has_refresh_token"])
        self.assertEqual(summary["administration_id"], "123")
        self.assertNotIn("access_token", summary)
        self.assertNotIn("refresh_token", summary)

    def test_describe_reports_an_absent_refresh_token(self) -> None:
        summary = OAuthConnection(access_token="at").describe()
        self.assertFalse(summary["has_refresh_token"])

    def test_store_errors_do_not_quote_the_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            path.write_text("{not json", encoding="utf-8")
            store = FileTokenStore(path)
            with self.assertRaises(MoneybirdError) as caught:
                store.save(self._connection())
        self._assert_clean(str(caught.exception))


class FileTokenStoreTests(_StoreCase):
    def test_round_trip_preserves_every_field(self) -> None:
        store = FileTokenStore()
        connection = OAuthConnection(
            access_token="at",
            refresh_token="rt",
            scope="bank settings",
            expires_in=7200,
            obtained_at=1000,
            administration_id="123",
        )
        store.save(connection)
        loaded = store.load()
        assert loaded is not None
        self.assertEqual(loaded.access_token, "at")
        self.assertEqual(loaded.refresh_token, "rt")
        self.assertEqual(loaded.scope, "bank settings")
        self.assertEqual(loaded.expires_in, 7200)
        self.assertEqual(loaded.obtained_at, 1000)
        self.assertEqual(loaded.administration_id, "123")

    def test_the_legacy_on_disk_shape_still_loads(self) -> None:
        """An existing moneybird_oauth_tokens.json must survive the upgrade."""
        path = Path(self._data_dir) / oauth_store.STORE_FILENAME
        path.write_text(
            json.dumps(
                {
                    "default": {
                        "access_token": "legacy-at",
                        "refresh_token": "legacy-rt",
                        "scope": "sales_invoices",
                        "obtained_at": 1234,
                    }
                }
            ),
            encoding="utf-8",
        )
        loaded = FileTokenStore().load()
        assert loaded is not None
        self.assertEqual(loaded.access_token, "legacy-at")
        self.assertEqual(loaded.refresh_token, "legacy-rt")
        self.assertIsNone(loaded.administration_id)

    def test_profiles_are_isolated_and_listable(self) -> None:
        store = FileTokenStore()
        store.save(OAuthConnection(access_token="at-default"))
        store.save(OAuthConnection(access_token="at-other"), profile="other")
        self.assertEqual(store.profiles(), ("default", "other"))
        self.assertEqual(store.load("other").access_token, "at-other")
        self.assertEqual(store.load().access_token, "at-default")

    def test_delete_removes_only_the_named_profile(self) -> None:
        store = FileTokenStore()
        store.save(OAuthConnection(access_token="a"))
        store.save(OAuthConnection(access_token="b"), profile="other")
        self.assertTrue(store.delete("other"))
        self.assertEqual(store.profiles(), ("default",))
        self.assertIsNone(store.load("other"))
        self.assertIsNotNone(store.load())

    def test_delete_reports_when_there_was_nothing_to_delete(self) -> None:
        self.assertFalse(FileTokenStore().delete("never-logged-in"))

    def test_deleting_the_last_profile_removes_the_file(self) -> None:
        store = FileTokenStore()
        store.save(OAuthConnection(access_token="a"))
        self.assertTrue(store.path.exists())
        self.assertTrue(store.delete())
        self.assertFalse(store.path.exists())

    def test_a_record_without_a_token_is_not_a_connection(self) -> None:
        path = Path(self._data_dir) / oauth_store.STORE_FILENAME
        path.write_text(json.dumps({"default": {"scope": "bank"}}), encoding="utf-8")
        self.assertIsNone(FileTokenStore().load())

    def test_a_corrupt_store_is_reported_not_silently_ignored(self) -> None:
        path = Path(self._data_dir) / oauth_store.STORE_FILENAME
        path.write_text("[]", encoding="utf-8")
        with self.assertRaises(MoneybirdError) as caught:
            FileTokenStore().load()
        self.assertIn("credential store", str(caught.exception))

    def test_an_unwritable_location_is_explained(self) -> None:
        store = FileTokenStore(Path(self._data_dir) / "missing-dir" / "tokens.json")
        with self.assertRaises(MoneybirdError) as caught:
            store.save(OAuthConnection(access_token="at"))
        message = str(caught.exception)
        self.assertIn("Could not write", message)
        self.assertIn("MONEYBIRD_MCP_DATA_DIR", message)

    def test_no_temporary_file_is_left_behind(self) -> None:
        store = FileTokenStore()
        store.save(OAuthConnection(access_token="at"))
        leftovers = [
            item.name
            for item in Path(self._data_dir).iterdir()
            if item.name.endswith(".tmp")
        ]
        self.assertEqual(leftovers, [])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not enforced on Windows")
    def test_the_credential_file_is_owner_only(self) -> None:
        store = FileTokenStore()
        store.save(OAuthConnection(access_token="at"))
        mode = stat.S_IMODE(store.path.stat().st_mode)
        self.assertEqual(mode & 0o077, 0)

    def test_the_store_is_replaceable_for_a_hosted_backend(self) -> None:
        """A hosted deployment swaps storage without touching the client."""

        class MemoryStore:
            def __init__(self) -> None:
                self.rows: dict[str, OAuthConnection] = {}

            def load(self, profile: str = "default") -> OAuthConnection | None:
                return self.rows.get(profile)

            def save(
                self, connection: OAuthConnection, *, profile: str = "default"
            ) -> None:
                self.rows[profile] = connection

            def delete(self, profile: str = "default") -> bool:
                return self.rows.pop(profile, None) is not None

            def profiles(self) -> tuple[str, ...]:
                return tuple(sorted(self.rows))

            def location(self) -> str:
                return "memory://tenant-rows"

        memory = MemoryStore()
        previous = oauth_store.set_token_store(memory)
        try:
            oauth.store_tokens({"access_token": "hosted-at"}, profile="tenant-7")
            self.assertEqual(oauth.get_access_token("tenant-7"), "hosted-at")
            # Reported verbatim: a hosted location is not a filesystem path.
            self.assertEqual(oauth.credential_location(), "memory://tenant-rows")
            self.assertIsNone(oauth.get_access_token("tenant-8"))
        finally:
            oauth_store.set_token_store(previous)


class TokenSessionTests(_StoreCase):
    def test_no_stored_tokens_returns_none(self) -> None:
        self.assertIsNone(oauth.get_access_token())
        self.assertIsNone(oauth.load_connection())
        self.assertIsNone(oauth.load_tokens())

    def test_non_expiring_token_round_trips(self) -> None:
        oauth.store_tokens({"access_token": "at", "refresh_token": "rt", "scope": "bank"})
        self.assertEqual(oauth.get_access_token(), "at")
        self.assertEqual(oauth.load_tokens()["scope"], "bank")

    def test_profiles_are_isolated(self) -> None:
        oauth.store_tokens({"access_token": "at-default"})
        oauth.store_tokens({"access_token": "at-other"}, profile="other")
        self.assertEqual(oauth.get_access_token(), "at-default")
        self.assertEqual(oauth.get_access_token("other"), "at-other")

    def test_a_valid_token_is_never_refreshed(self) -> None:
        oauth.store_tokens(
            {
                "access_token": "at",
                "refresh_token": "rt",
                "expires_in": 7200,
                "obtained_at": int(time.time()),
            }
        )
        with mock.patch.object(oauth, "refresh_access_token") as refresh:
            for _ in range(5):
                self.assertEqual(oauth.get_access_token(), "at")
        refresh.assert_not_called()

    def test_a_token_without_expiry_metadata_is_never_refreshed(self) -> None:
        oauth.store_tokens({"access_token": "at", "refresh_token": "rt"})
        with mock.patch.object(oauth, "refresh_access_token") as refresh:
            self.assertEqual(oauth.get_access_token(), "at")
        refresh.assert_not_called()

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

    def test_refresh_keeps_the_selected_administration_and_scope(self) -> None:
        oauth.store_tokens(
            {
                "access_token": "stale",
                "refresh_token": "rt",
                "scope": "bank settings",
                "expires_in": 60,
                "obtained_at": int(time.time()) - 600,
            }
        )
        connection = oauth.load_connection()
        oauth.save_connection(connection.with_administration("123"))

        with mock.patch.object(
            oauth, "refresh_access_token", return_value={"access_token": "fresh"}
        ):
            oauth.get_access_token()

        stored = oauth.load_connection()
        self.assertEqual(stored.access_token, "fresh")
        self.assertEqual(stored.refresh_token, "rt")  # not cleared
        self.assertEqual(stored.scope, "bank settings")
        self.assertEqual(stored.administration_id, "123")

    def test_a_failed_refresh_leaves_the_stored_credentials_intact(self) -> None:
        """A network blip must not cost the user their refresh token."""
        oauth.store_tokens(
            {
                "access_token": "stale",
                "refresh_token": "rt-precious",
                "expires_in": 60,
                "obtained_at": int(time.time()) - 600,
            }
        )
        with mock.patch.object(
            oauth,
            "refresh_access_token",
            side_effect=MoneybirdError("Could not reach the Moneybird token endpoint."),
        ):
            with self.assertRaises(MoneybirdError) as caught:
                oauth.get_access_token()

        message = str(caught.exception)
        self.assertIn("left", message)
        self.assertIn("auth login", message)
        stored = oauth.load_connection()
        self.assertEqual(stored.access_token, "stale")
        self.assertEqual(stored.refresh_token, "rt-precious")

    def test_a_revoked_grant_produces_a_reauthentication_message(self) -> None:
        oauth.store_tokens(
            {
                "access_token": "stale",
                "refresh_token": "rt",
                "expires_in": 60,
                "obtained_at": int(time.time()) - 600,
            }
        )
        upstream = urllib.error.HTTPError(
            oauth.OAUTH_TOKEN_URL,
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"error":"invalid_grant","error_description":"revoked"}'),
        )
        with mock.patch.object(urllib.request, "urlopen", side_effect=upstream):
            with self.assertRaises(MoneybirdError) as caught:
                oauth.get_access_token()
        message = str(caught.exception)
        self.assertIn("auth login", message)
        self.assertIsNotNone(oauth.load_connection())

    def test_expired_token_without_refresh_token_raises(self) -> None:
        oauth.store_tokens(
            {
                "access_token": "stale",
                "expires_in": 60,
                "obtained_at": int(time.time()) - 3600,
            }
        )
        with self.assertRaises(MoneybirdError) as caught:
            oauth.get_access_token()
        self.assertIn("auth login", str(caught.exception))

    def test_relogin_preserves_a_previously_selected_administration(self) -> None:
        oauth.store_tokens({"access_token": "at-1"})
        oauth.save_connection(oauth.load_connection().with_administration("123"))
        oauth.store_tokens({"access_token": "at-2"})
        self.assertEqual(oauth.get_administration_id(), "123")
        self.assertEqual(oauth.get_access_token(), "at-2")

    def test_delete_connection_removes_the_stored_identity(self) -> None:
        oauth.store_tokens({"access_token": "at"})
        self.assertTrue(oauth.delete_connection())
        self.assertIsNone(oauth.get_access_token())
        self.assertFalse(oauth.delete_connection())

    def test_moneybird_documents_no_revocation_endpoint(self) -> None:
        """Pinned so a future change to this claim has to be deliberate."""
        self.assertFalse(oauth.REVOCATION_SUPPORTED)


class CredentialFallbackTests(_StoreCase):
    def test_oauth_store_is_used_when_env_token_is_absent(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MONEYBIRD_ACCESS_TOKEN": "", "MONEYBIRD_ADMINISTRATION_ID": "123"},
        ):
            with mock.patch.object(
                oauth,
                "get_connection",
                return_value=OAuthConnection(access_token="oauth-at"),
            ):
                resolved = credentials.resolve_credentials()
        self.assertEqual(resolved.source, "oauth")
        self.assertEqual(resolved.token, "oauth-at")
        self.assertEqual(resolved.administration_id, "123")

    def test_env_token_still_wins_over_oauth_store(self) -> None:
        """Deterministic precedence: a personal token is never silently replaced."""
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "env-at"}):
            with mock.patch.object(
                oauth,
                "get_connection",
                return_value=OAuthConnection(access_token="oauth-at"),
            ) as connection:
                resolved = credentials.resolve_credentials()
        self.assertEqual(resolved.source, "environment")
        self.assertEqual(resolved.token, "env-at")
        # The OAuth store is not even consulted, so a stale stored token cannot
        # trigger a refresh for an installation that uses a personal token.
        connection.assert_not_called()

    def test_the_administration_chosen_at_login_is_used(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"MONEYBIRD_ACCESS_TOKEN": "", "MONEYBIRD_ADMINISTRATION_ID": ""},
        ):
            with mock.patch.object(
                oauth,
                "get_connection",
                return_value=OAuthConnection(
                    access_token="oauth-at", administration_id="777"
                ),
            ):
                resolved = credentials.resolve_credentials()
        self.assertEqual(resolved.administration_id, "777")

    def test_an_explicit_environment_administration_overrides_the_stored_one(
        self,
    ) -> None:
        with mock.patch.dict(
            os.environ,
            {"MONEYBIRD_ACCESS_TOKEN": "", "MONEYBIRD_ADMINISTRATION_ID": "999"},
        ):
            with mock.patch.object(
                oauth,
                "get_connection",
                return_value=OAuthConnection(
                    access_token="oauth-at", administration_id="777"
                ),
            ):
                resolved = credentials.resolve_credentials()
        self.assertEqual(resolved.administration_id, "999")

    def test_no_credentials_anywhere_raises_with_oauth_hint(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": ""}):
            with mock.patch.object(oauth, "get_connection", return_value=None):
                with self.assertRaises(MoneybirdError) as ctx:
                    credentials.resolve_credentials()
        self.assertIn("auth login", str(ctx.exception))

    def test_hosted_request_mode_never_reads_the_local_oauth_store(self) -> None:
        oauth.store_tokens({"access_token": "local-at"})
        with mock.patch.object(credentials, "_request_headers", return_value={}):
            with self.assertRaises(MoneybirdError):
                credentials.resolve_credentials("hosted_request_only")


class NoAccidentalNetworkTests(_StoreCase):
    """Pure operations must never reach the network."""

    def test_url_building_storage_and_status_paths_make_no_request(self) -> None:
        def explode(*_: object, **__: object) -> None:
            raise AssertionError("a unit test attempted a network request")

        with mock.patch.object(urllib.request, "urlopen", side_effect=explode):
            oauth.build_authorize_url()
            oauth.generate_state()
            oauth_scopes.parse_scopes("bookkeeping")
            oauth.store_tokens({"access_token": "at", "refresh_token": "rt"})
            self.assertEqual(oauth.get_access_token(), "at")
            self.assertIsNotNone(oauth.load_connection())
            self.assertIsNone(oauth.get_administration_id())
            credentials.credentials_are_configured("local")
            self.assertTrue(oauth.delete_connection())


if __name__ == "__main__":
    unittest.main()
