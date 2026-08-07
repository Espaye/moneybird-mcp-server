"""Tests for `moneybird-mcp auth login | status | logout`.

Everything Moneybird-facing is mocked: no client id, client secret, account, or
network access is needed. `input()` and `webbrowser.open` are patched too, so no
test can block on a prompt or open a browser on the machine running CI.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird_mcp import auth_cli, oauth, oauth_store, server
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.oauth_store import FileTokenStore

FAKE_APP = {
    "MONEYBIRD_OAUTH_CLIENT_ID": "client-id-123",
    "MONEYBIRD_OAUTH_CLIENT_SECRET": "client-secret-456",
}

# Deliberately distinctive so an assertion can prove they never reach output.
FAKE_TOKENS = {
    "access_token": "AT-NEVER-PRINT",
    "refresh_token": "RT-NEVER-PRINT",
    "token_type": "bearer",
    "scope": "sales_invoices documents estimates bank time_entries settings",
}

@contextlib.contextmanager
def _without(*names: str):
    """Remove variables entirely, not merely blank them.

    `load_env_file` uses os.environ.setdefault, so a variable present-but-empty
    is still "already set" and the file cannot supply it. Emptying rather than
    removing would therefore test the wrong thing.
    """
    saved = {name: os.environ.pop(name, None) for name in names}
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


ONE_ADMINISTRATION = [{"id": 123, "name": "Test BV"}]
TWO_ADMINISTRATIONS = [
    {"id": 123, "name": "Test BV"},
    {"id": 456, "name": "Holding BV"},
]


class _CliCase(unittest.TestCase):
    def setUp(self) -> None:
        self._data_dir = tempfile.mkdtemp(prefix="moneybird_cli_test_")
        environment = mock.patch.dict(
            os.environ,
            {
                "MONEYBIRD_MCP_DATA_DIR": self._data_dir,
                "MONEYBIRD_ACCESS_TOKEN": "",
                "MONEYBIRD_ADMINISTRATION_ID": "",
                "MONEYBIRD_CREDENTIAL_MODE": "local",
                "MONEYBIRD_OAUTH_SCOPES": "",
                **FAKE_APP,
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

        store = mock.patch.object(oauth_store, "_store", FileTokenStore())
        store.start()
        self.addCleanup(store.stop)

        # A browser must never open during a test run.
        browser = mock.patch.object(auth_cli.webbrowser, "open")
        self.browser = browser.start()
        self.addCleanup(browser.stop)

    def run_cli(
        self,
        argv: list[str],
        *,
        answers: list[str] | None = None,
    ) -> tuple[int, str, str]:
        """Run the CLI with scripted stdin answers; return (code, stdout, stderr)."""
        responses = list(answers or [])

        def fake_input(_prompt: str = "") -> str:
            if not responses:
                raise EOFError
            return responses.pop(0)

        out, err = io.StringIO(), io.StringIO()
        with (
            mock.patch.object(auth_cli, "input", fake_input, create=True),
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
        ):
            code = auth_cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def assert_no_secrets(self, *texts: str) -> None:
        blob = "\n".join(texts)
        for secret in (
            FAKE_TOKENS["access_token"],
            FAKE_TOKENS["refresh_token"],
            FAKE_APP["MONEYBIRD_OAUTH_CLIENT_SECRET"],
        ):
            self.assertNotIn(secret, blob)


class LoginTests(_CliCase):
    def _login(
        self,
        argv: list[str],
        *,
        answers: list[str],
        administrations: list[dict[str, object]] | None = None,
        tokens: dict[str, object] | None = None,
    ) -> tuple[int, str, str, mock.MagicMock]:
        with (
            mock.patch.object(
                oauth, "exchange_authorization_code", return_value=tokens or FAKE_TOKENS
            ) as exchange,
            mock.patch.object(
                auth_cli,
                "_verify_connection",
                return_value=(
                    ONE_ADMINISTRATION if administrations is None else administrations
                ),
            ),
        ):
            code, out, err = self.run_cli(argv, answers=answers)
        return code, out, err, exchange

    def test_login_prints_the_url_exchanges_and_stores(self) -> None:
        code, out, err, exchange = self._login(
            ["login"], answers=["the-auth-code"]
        )
        self.assertEqual(code, 0, err)
        self.assertIn("https://moneybird.com/oauth/authorize", out)
        self.assertIn("client_id=client-id-123", out)
        self.assertIn("redirect_uri=urn%3Aietf%3Awg%3Aoauth%3A2.0%3Aoob", out)
        exchange.assert_called_once_with(
            "the-auth-code", redirect_uri=oauth.OOB_REDIRECT_URI
        )
        stored = oauth.load_connection()
        assert stored is not None
        self.assertEqual(stored.access_token, FAKE_TOKENS["access_token"])
        self.assertEqual(stored.refresh_token, FAKE_TOKENS["refresh_token"])

    def test_login_never_prints_a_token_or_the_client_secret(self) -> None:
        _, out, err, _ = self._login(["login"], answers=["code"])
        self.assert_no_secrets(out, err)
        # It still says a refresh token was stored, without showing it.
        self.assertIn("refresh token", out.lower())

    def test_login_prints_the_url_even_when_a_browser_opens(self) -> None:
        """Headless and remote hosts only ever get the printed URL."""
        _, out, _, _ = self._login(["login"], answers=["code"])
        self.browser.assert_called_once()
        self.assertIn("moneybird.com/oauth/authorize", out)

    def test_no_browser_flag_suppresses_the_launch(self) -> None:
        _, out, _, _ = self._login(["login", "--no-browser"], answers=["code"])
        self.browser.assert_not_called()
        self.assertIn("moneybird.com/oauth/authorize", out)

    def test_login_reports_the_requested_scopes_before_authorizing(self) -> None:
        _, out, _, _ = self._login(["login"], answers=["code"])
        self.assertIn("Requesting scopes:", out)
        for scope in ("sales_invoices", "documents", "bank", "settings"):
            self.assertIn(scope, out)

    def test_a_scope_profile_narrows_the_request(self) -> None:
        _, out, _, _ = self._login(
            ["login", "--scopes", "invoicing"], answers=["code"]
        )
        self.assertIn("scope=sales_invoices+settings", out)

    def test_an_unknown_scope_fails_before_the_browser_opens(self) -> None:
        code, _, err = self.run_cli(["login", "--scopes", "everything"])
        self.assertEqual(code, 1)
        self.assertIn("everything", err)
        self.browser.assert_not_called()

    def test_missing_client_credentials_fail_before_the_browser_opens(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_OAUTH_CLIENT_SECRET": ""}):
            code, _, err = self.run_cli(["login"])
        self.assertEqual(code, 1)
        self.assertIn("MONEYBIRD_OAUTH_CLIENT_SECRET", err)
        self.browser.assert_not_called()

    def test_an_empty_code_stores_nothing(self) -> None:
        with mock.patch.object(oauth, "exchange_authorization_code") as exchange:
            code, _, err = self.run_cli(["login"], answers=[""])
        self.assertEqual(code, 1)
        exchange.assert_not_called()
        self.assertIsNone(oauth.load_connection())
        self.assertIn("nothing was stored", err)

    def test_an_aborted_prompt_stores_nothing(self) -> None:
        with mock.patch.object(oauth, "exchange_authorization_code") as exchange:
            code, _, err = self.run_cli(["login"], answers=[])  # EOF
        self.assertEqual(code, 1)
        exchange.assert_not_called()
        self.assertIsNone(oauth.load_connection())

    def test_a_rejected_code_is_reported_and_stores_nothing(self) -> None:
        with mock.patch.object(
            oauth,
            "exchange_authorization_code",
            side_effect=MoneybirdError(
                "Moneybird token request failed with HTTP 400. invalid_grant"
            ),
        ):
            code, _, err = self.run_cli(["login"], answers=["stale-code"])
        self.assertEqual(code, 1)
        self.assertIn("invalid_grant", err)
        self.assertIsNone(oauth.load_connection())

    def test_a_withheld_scope_is_reported_as_a_warning(self) -> None:
        _, out, _, _ = self._login(
            ["login"],
            answers=["code"],
            tokens={**FAKE_TOKENS, "scope": "sales_invoices"},
        )
        self.assertIn("did not grant", out)
        self.assertIn("bank", out)

    def test_a_failed_verification_keeps_the_stored_connection(self) -> None:
        """The grant is already spent; discarding it would cost another round trip."""
        with (
            mock.patch.object(
                oauth, "exchange_authorization_code", return_value=FAKE_TOKENS
            ),
            mock.patch.object(
                auth_cli,
                "_verify_connection",
                side_effect=MoneybirdError("Moneybird is unreachable"),
            ),
        ):
            code, _, err = self.run_cli(["login"], answers=["code"])
        self.assertEqual(code, 1)
        self.assertIn("stored, but verifying it failed", err)
        self.assertIsNotNone(oauth.load_connection())

    # --- Administration selection --------------------------------------------

    def test_a_single_administration_is_selected_automatically(self) -> None:
        _, out, _, _ = self._login(["login"], answers=["code"])
        self.assertIn("only available administration", out)
        self.assertEqual(oauth.get_administration_id(), "123")

    def test_several_administrations_are_offered_interactively(self) -> None:
        with mock.patch("sys.stdin.isatty", return_value=True):
            _, out, _, _ = self._login(
                ["login"],
                answers=["code", "2"],
                administrations=TWO_ADMINISTRATIONS,
            )
        self.assertIn("1. Test BV (id 123)", out)
        self.assertIn("2. Holding BV (id 456)", out)
        self.assertEqual(oauth.get_administration_id(), "456")

    def test_an_invalid_choice_is_re_prompted(self) -> None:
        with mock.patch("sys.stdin.isatty", return_value=True):
            _, out, _, _ = self._login(
                ["login"],
                answers=["code", "9", "1"],
                administrations=TWO_ADMINISTRATIONS,
            )
        self.assertIn("Not one of the options", out)
        self.assertEqual(oauth.get_administration_id(), "123")

    def test_skipping_the_choice_stores_no_administration(self) -> None:
        """Guessing would silently point every later write at the wrong books."""
        with mock.patch("sys.stdin.isatty", return_value=True):
            _, out, _, _ = self._login(
                ["login"],
                answers=["code", ""],
                administrations=TWO_ADMINISTRATIONS,
            )
        self.assertIsNone(oauth.get_administration_id())
        self.assertIn("MONEYBIRD_ADMINISTRATION_ID", out)

    def test_non_interactive_login_lists_them_and_selects_none(self) -> None:
        with mock.patch("sys.stdin.isatty", return_value=False):
            _, out, _, _ = self._login(
                ["login"], answers=["code"], administrations=TWO_ADMINISTRATIONS
            )
        self.assertIn("not running interactively", out)
        self.assertIn("--administration ID", out)
        self.assertIsNone(oauth.get_administration_id())

    def test_an_explicit_administration_flag_skips_the_prompt(self) -> None:
        _, out, _, _ = self._login(
            ["login", "--administration", "456"],
            answers=["code"],
            administrations=TWO_ADMINISTRATIONS,
        )
        self.assertEqual(oauth.get_administration_id(), "456")
        self.assertNotIn("Select an administration", out)

    def test_an_inaccessible_administration_is_refused(self) -> None:
        code, _, err = self._login(
            ["login", "--administration", "999"],
            answers=["code"],
            administrations=TWO_ADMINISTRATIONS,
        )[:3]
        self.assertEqual(code, 1)
        self.assertIn("not accessible", err)
        # The tokens themselves are valid, so they stay.
        self.assertIsNotNone(oauth.load_connection())
        self.assertIsNone(oauth.get_administration_id())

    def test_a_connection_with_no_administrations_warns(self) -> None:
        _, out, _, _ = self._login(["login"], answers=["code"], administrations=[])
        self.assertIn("no administrations", out)

    def test_a_malformed_administration_response_is_a_message_not_a_traceback(
        self,
    ) -> None:
        """This runs after the code is spent; a traceback here helps nobody."""
        from moneybird_mcp import client as client_module

        for payload in ({"unexpected": "shape"}, None, "administrations"):
            with self.subTest(payload=payload):
                with mock.patch.object(
                    client_module.MoneybirdClient,
                    "list_administrations",
                    return_value=payload,
                ):
                    with self.assertRaises(MoneybirdError):
                        auth_cli._verify_connection("token")

    def test_administrations_without_an_id_are_dropped(self) -> None:
        from moneybird_mcp import client as client_module

        with mock.patch.object(
            client_module.MoneybirdClient,
            "list_administrations",
            return_value=[{"name": "No id"}, {"id": 5, "name": "Real"}, "junk"],
        ):
            self.assertEqual(
                auth_cli._verify_connection("token"), [{"id": 5, "name": "Real"}]
            )

    # --- Profiles and callback mode -------------------------------------------

    def test_a_named_profile_is_isolated(self) -> None:
        self._login(["login", "--profile", "second"], answers=["code"])
        self.assertIsNone(oauth.load_connection())
        self.assertIsNotNone(oauth.load_connection("second"))

    def test_an_explicit_redirect_uri_takes_a_callback_url(self) -> None:
        """The same command serves a registered HTTPS callback during development."""
        with (
            mock.patch.object(
                oauth, "exchange_authorization_code", return_value=FAKE_TOKENS
            ) as exchange,
            mock.patch.object(
                auth_cli, "_verify_connection", return_value=ONE_ADMINISTRATION
            ),
        ):
            code, out, err = self.run_cli(
                ["login", "--redirect-uri", "https://app.example/cb"],
                answers=["https://app.example/cb?code=xyz789"],
            )
        self.assertEqual(code, 0, err)
        exchange.assert_called_once_with(
            "xyz789", redirect_uri="https://app.example/cb"
        )


class StatusTests(_CliCase):
    def test_status_without_any_credentials_says_so(self) -> None:
        code, out, _ = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("none stored", out)
        self.assertIn("Active identity:        none", out)
        self.assertIn("auth login", out)

    def test_status_reports_a_stored_connection_without_tokens(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        oauth.save_connection(oauth.load_connection().with_administration("123"))
        code, out, err = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assert_no_secrets(out, err)
        self.assertIn("stored for profile 'default'", out)
        self.assertIn("sales_invoices", out)  # granted scopes are not secret
        self.assertIn("refresh token:        stored", out)
        self.assertIn("administration:       123", out)

    def test_status_shows_the_client_id_but_never_the_secret(self) -> None:
        code, out, err = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("client id client-id-123", out)  # not a secret
        self.assertIn("secret set", out)
        self.assert_no_secrets(out, err)

    def test_status_names_the_personal_token_as_the_winner(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "personal"}):
            code, out, err = self.run_cli(["status"])
        self.assertEqual(code, 0)
        self.assertIn("Active identity:        the personal API token", out)
        self.assertIn("takes precedence", out)
        self.assertNotIn("personal\n", out)  # the token value itself is never shown

    def test_status_flags_an_environment_administration_override(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        oauth.save_connection(oauth.load_connection().with_administration("123"))
        with mock.patch.dict(os.environ, {"MONEYBIRD_ADMINISTRATION_ID": "456"}):
            _, out, _ = self.run_cli(["status"])
        self.assertIn("overrides the stored", out)
        self.assertIn("456", out)

    def test_status_reports_an_expired_access_token(self) -> None:
        oauth.store_tokens(
            {**FAKE_TOKENS, "expires_in": 60, "obtained_at": 1_000_000}
        )
        _, out, _ = self.run_cli(["status"])
        self.assertIn("EXPIRED", out)

    def test_status_never_refreshes_or_contacts_moneybird(self) -> None:
        oauth.store_tokens(
            {**FAKE_TOKENS, "expires_in": 60, "obtained_at": 1_000_000}
        )
        with (
            mock.patch.object(oauth, "refresh_access_token") as refresh,
            mock.patch.object(oauth, "get_connection") as resolve,
        ):
            code, _, _ = self.run_cli(["status"])
        self.assertEqual(code, 0)
        refresh.assert_not_called()
        resolve.assert_not_called()

    def test_status_reports_a_corrupt_store_instead_of_crashing(self) -> None:
        path = Path(self._data_dir) / oauth_store.STORE_FILENAME
        path.write_text("[]", encoding="utf-8")
        code, _, err = self.run_cli(["status"])
        self.assertEqual(code, 1)
        self.assertIn("could not be read", err)


class LogoutTests(_CliCase):
    def test_logout_deletes_the_local_connection(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        code, out, _ = self.run_cli(["logout"])
        self.assertEqual(code, 0)
        self.assertIsNone(oauth.load_connection())
        self.assertIn("deleted", out)

    def test_logout_distinguishes_deletion_from_revocation(self) -> None:
        """Moneybird documents no revocation endpoint; saying otherwise misleads."""
        oauth.store_tokens(FAKE_TOKENS)
        _, out, _ = self.run_cli(["logout"])
        self.assertIn("local credentials only", out)
        self.assertIn("https://moneybird.com/user/applications", out)

    def test_logout_without_a_connection_is_not_an_error(self) -> None:
        code, out, _ = self.run_cli(["logout"])
        self.assertEqual(code, 0)
        self.assertIn("No stored OAuth credentials", out)

    def test_logout_leaves_other_profiles_alone(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        oauth.store_tokens(FAKE_TOKENS, profile="second")
        self.run_cli(["logout", "--profile", "second"])
        self.assertIsNotNone(oauth.load_connection())
        self.assertIsNone(oauth.load_connection("second"))

    def test_logout_leaves_a_personal_api_token_untouched(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "personal"}):
            self.run_cli(["logout"])
            self.assertEqual(os.environ["MONEYBIRD_ACCESS_TOKEN"], "personal")


class ConsoleEncodingTests(_CliCase):
    """Output must survive a legacy Windows console code page.

    stdout on Windows uses the locale encoding below Python 3.15, and cp437 has
    no em dash. A single non-ASCII character in a printed string would raise
    UnicodeEncodeError partway through a login, after the grant was consumed.
    """

    def _assert_ascii(self, *texts: str) -> None:
        for text in texts:
            for line in text.splitlines():
                offenders = sorted({char for char in line if ord(char) > 127})
                self.assertEqual(offenders, [], f"non-ASCII in CLI output: {line!r}")

    def test_login_output_is_ascii(self) -> None:
        with (
            mock.patch.object(
                oauth, "exchange_authorization_code", return_value=FAKE_TOKENS
            ),
            mock.patch.object(
                auth_cli, "_verify_connection", return_value=TWO_ADMINISTRATIONS
            ),
            mock.patch("sys.stdin.isatty", return_value=True),
        ):
            _, out, err = self.run_cli(["login"], answers=["code", "1"])
        self._assert_ascii(out, err)

    def test_status_logout_and_scopes_output_is_ascii(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        for argv in (["status"], ["scopes"], ["logout"], ["status"]):
            with self.subTest(argv=argv):
                _, out, err = self.run_cli(argv)
                self._assert_ascii(out, err)

    def test_login_failure_messages_are_ascii(self) -> None:
        with mock.patch.object(
            oauth,
            "exchange_authorization_code",
            side_effect=MoneybirdError("Moneybird token request failed with HTTP 400."),
        ):
            _, out, err = self.run_cli(["login"], answers=["code"])
        self._assert_ascii(out, err)


class ScopesCommandTests(_CliCase):
    def test_scopes_command_explains_every_requested_scope(self) -> None:
        code, out, _ = self.run_cli(["scopes"])
        self.assertEqual(code, 0)
        for scope in (
            "sales_invoices",
            "documents",
            "estimates",
            "bank",
            "time_entries",
            "settings",
        ):
            self.assertIn(scope, out)

    def test_scopes_command_separates_scopes_from_write_policy(self) -> None:
        _, out, _ = self.run_cli(["scopes"])
        self.assertIn("MONEYBIRD_CAPABILITY_MODE", out)
        self.assertIn("no read-only", out)


class EnvFileTests(_CliCase):
    def test_an_explicit_env_file_supplies_the_application_credentials(self) -> None:
        env_path = Path(self._data_dir) / "operator.env"
        env_path.write_text(
            "MONEYBIRD_OAUTH_CLIENT_ID=from-file\n"
            "MONEYBIRD_OAUTH_CLIENT_SECRET=secret-from-file\n",
            encoding="utf-8",
        )
        with _without(
            "MONEYBIRD_OAUTH_CLIENT_ID", "MONEYBIRD_OAUTH_CLIENT_SECRET"
        ):
            with (
                mock.patch.object(
                    oauth, "exchange_authorization_code", return_value=FAKE_TOKENS
                ),
                mock.patch.object(
                    auth_cli, "_verify_connection", return_value=ONE_ADMINISTRATION
                ),
            ):
                code, out, err = self.run_cli(
                    ["login", "--env-file", str(env_path)], answers=["code"]
                )
        self.assertEqual(code, 0, err)
        self.assertIn("client_id=from-file", out)
        self.assertNotIn("secret-from-file", out + err)

    def test_the_parent_environment_still_wins_over_the_file(self) -> None:
        env_path = Path(self._data_dir) / "operator.env"
        env_path.write_text("MONEYBIRD_OAUTH_CLIENT_ID=from-file\n", encoding="utf-8")
        with (
            mock.patch.object(
                oauth, "exchange_authorization_code", return_value=FAKE_TOKENS
            ),
            mock.patch.object(
                auth_cli, "_verify_connection", return_value=ONE_ADMINISTRATION
            ),
        ):
            _, out, _ = self.run_cli(
                ["login", "--env-file", str(env_path)], answers=["code"]
            )
        self.assertIn("client_id=client-id-123", out)

    def test_no_env_file_is_discovered_automatically(self) -> None:
        """A working-directory .env must never be picked up implicitly."""
        with tempfile.TemporaryDirectory() as cwd:
            (Path(cwd) / ".env").write_text(
                "MONEYBIRD_OAUTH_CLIENT_ID=sneaky\n"
                "MONEYBIRD_OAUTH_CLIENT_SECRET=sneaky\n",
                encoding="utf-8",
            )
            previous = os.getcwd()
            os.chdir(cwd)
            try:
                with mock.patch.dict(
                    os.environ,
                    {
                        "MONEYBIRD_OAUTH_CLIENT_ID": "",
                        "MONEYBIRD_OAUTH_CLIENT_SECRET": "",
                    },
                ):
                    code, out, err = self.run_cli(["login"])
            finally:
                os.chdir(previous)
        self.assertEqual(code, 1)
        self.assertNotIn("sneaky", out + err)
        self.assertIn("MONEYBIRD_OAUTH_CLIENT_ID", err)

    def test_a_missing_env_file_is_a_usage_error(self) -> None:
        with self.assertRaises(SystemExit) as caught:
            self.run_cli(["login", "--env-file", str(Path(self._data_dir) / "nope.env")])
        self.assertEqual(caught.exception.code, 2)


class ConsoleScriptDispatchTests(_CliCase):
    """`moneybird-mcp auth ...` must reach the CLI without starting a server."""

    def test_the_auth_subcommand_is_dispatched_before_server_configuration(
        self,
    ) -> None:
        with mock.patch.object(auth_cli, "main", return_value=0) as cli:
            with mock.patch.object(server, "build_config") as build:
                with self.assertRaises(SystemExit) as caught:
                    server.main(["auth", "status", "--profile", "x"])
        self.assertEqual(caught.exception.code, 0)
        cli.assert_called_once_with(["status", "--profile", "x"])
        build.assert_not_called()

    def test_the_exit_code_is_propagated(self) -> None:
        with mock.patch.object(auth_cli, "main", return_value=1):
            with self.assertRaises(SystemExit) as caught:
                server.main(["auth", "status"])
        self.assertEqual(caught.exception.code, 1)

    def test_server_arguments_are_unaffected(self) -> None:
        """No existing invocation becomes ambiguous by adding a positional."""
        config = server.build_config(["--tool-discovery", "search"])
        self.assertEqual(config.tool_discovery, "search")
        self.assertEqual(config.transport, "stdio")

    def test_server_help_mentions_the_auth_commands(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            with self.assertRaises(SystemExit):
                server.build_config(["--help"])
        self.assertIn("moneybird-mcp auth login", out.getvalue())

    def test_the_legacy_oauth_login_module_still_runs_a_login(self) -> None:
        from moneybird_mcp import oauth_login

        with mock.patch.object(auth_cli, "main", return_value=0) as cli:
            self.assertEqual(oauth_login.main(["--no-browser"]), 0)
        cli.assert_called_once_with(["login", "--no-browser"])


class DataDirectoryTests(_CliCase):
    def test_the_default_state_root_matches_the_stdio_server(self) -> None:
        """Logging in elsewhere stores a connection the server never finds."""
        with mock.patch.dict(os.environ, {"MONEYBIRD_MCP_DATA_DIR": ""}):
            with mock.patch.object(auth_cli.Path, "home", return_value=Path("/home/x")):
                auth_cli._default_data_dir()
                self.assertEqual(
                    Path(os.environ["MONEYBIRD_MCP_DATA_DIR"]),
                    Path("/home/x/.moneybird-mcp"),
                )

    def test_an_explicit_state_root_is_respected(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_MCP_DATA_DIR": "/custom"}):
            auth_cli._default_data_dir()
            self.assertEqual(os.environ["MONEYBIRD_MCP_DATA_DIR"], "/custom")

    def test_login_reports_where_the_connection_was_stored(self) -> None:
        with (
            mock.patch.object(
                oauth, "exchange_authorization_code", return_value=FAKE_TOKENS
            ),
            mock.patch.object(
                auth_cli, "_verify_connection", return_value=ONE_ADMINISTRATION
            ),
        ):
            _, out, _ = self.run_cli(["login"], answers=["code"])
        self.assertIn(oauth_store.STORE_FILENAME, out)

    def test_the_stored_file_contains_no_plaintext_beyond_the_grant(self) -> None:
        oauth.store_tokens(FAKE_TOKENS)
        path = Path(self._data_dir) / oauth_store.STORE_FILENAME
        record = json.loads(path.read_text(encoding="utf-8"))["default"]
        self.assertNotIn(
            FAKE_APP["MONEYBIRD_OAUTH_CLIENT_SECRET"], json.dumps(record)
        )


if __name__ == "__main__":
    unittest.main()
