"""Credential-mode containment tests."""
from __future__ import annotations

import importlib
import os
import unittest
from unittest import mock

import fastmcp.server.dependencies as dependencies
from starlette.testclient import TestClient

from moneybird_mcp import oauth
from moneybird_mcp.auth import SharedSecretAuthMiddleware
from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    CREDENTIAL_MODE_LOCAL,
    CREDENTIAL_MODE_NETWORK_SINGLE_USER,
    CredentialModeMiddleware,
    credentials_are_configured,
    resolve_credentials,
)
from moneybird_mcp.oauth_store import OAuthConnection


class CredentialResolutionModeTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            dependencies,
            "get_http_headers",
            return_value={},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_hosted_mode_uses_only_nonblank_request_credentials(self) -> None:
        dependencies.get_http_headers.return_value = {
            "X-Moneybird-Token": "tenant-token",
            "X-Moneybird-Administration-Id": "42",
        }
        with (
            mock.patch.dict(
                os.environ,
                {
                    "MONEYBIRD_ACCESS_TOKEN": "operator-token",
                    "MONEYBIRD_ADMINISTRATION_ID": "999",
                },
            ),
            mock.patch.object(oauth, "get_connection") as stored_oauth,
        ):
            resolved = resolve_credentials(CREDENTIAL_MODE_HOSTED_REQUEST_ONLY)

        self.assertEqual(resolved.source, "request")
        self.assertEqual(resolved.token, "tenant-token")
        self.assertEqual(resolved.administration_id, "42")
        stored_oauth.assert_not_called()

    def test_hosted_mode_missing_context_never_falls_back(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "MONEYBIRD_ACCESS_TOKEN": "operator-token",
                    "MONEYBIRD_ADMINISTRATION_ID": "999",
                },
            ),
            mock.patch.object(oauth, "get_connection") as stored_oauth,
        ):
            with self.assertRaisesRegex(MoneybirdError, "Hosted request credentials"):
                resolve_credentials(CREDENTIAL_MODE_HOSTED_REQUEST_ONLY)

        stored_oauth.assert_not_called()

    def test_hosted_mode_blank_context_never_falls_back(self) -> None:
        dependencies.get_http_headers.return_value = {
            "X-Moneybird-Token": "   ",
            "X-Moneybird-Administration-Id": "42",
        }
        with (
            mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "operator-token"}),
            mock.patch.object(oauth, "get_connection") as stored_oauth,
        ):
            with self.assertRaisesRegex(MoneybirdError, "Hosted request credentials"):
                resolve_credentials(CREDENTIAL_MODE_HOSTED_REQUEST_ONLY)

        stored_oauth.assert_not_called()

    def test_network_single_user_uses_local_identity_without_tenant_headers(self) -> None:
        with (
            mock.patch.dict(
                os.environ,
                {
                    "MONEYBIRD_ACCESS_TOKEN": "operator-token",
                    "MONEYBIRD_ADMINISTRATION_ID": "7",
                },
            ),
            mock.patch.object(oauth, "get_connection") as stored_oauth,
        ):
            resolved = resolve_credentials(CREDENTIAL_MODE_NETWORK_SINGLE_USER)

        self.assertEqual(resolved.source, "environment")
        self.assertEqual(resolved.token, "operator-token")
        self.assertEqual(resolved.administration_id, "7")
        stored_oauth.assert_not_called()

    def test_network_single_user_rejects_any_tenant_switch_header(self) -> None:
        cases = (
            {"X-Moneybird-Token": "other-tenant"},
            {"X-Moneybird-Token": "   "},
            {"X-Moneybird-Administration-Id": "other-administration"},
        )
        for headers in cases:
            with self.subTest(headers=headers):
                dependencies.get_http_headers.return_value = headers
                with (
                    mock.patch.dict(
                        os.environ,
                        {"MONEYBIRD_ACCESS_TOKEN": "operator-token"},
                    ),
                    mock.patch.object(oauth, "get_connection") as stored_oauth,
                ):
                    with self.assertRaisesRegex(
                        MoneybirdError, "tenant headers are not allowed"
                    ):
                        resolve_credentials(CREDENTIAL_MODE_NETWORK_SINGLE_USER)
                stored_oauth.assert_not_called()

    def test_invalid_mode_fails_before_credentials_are_considered(self) -> None:
        with mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "operator-token"}):
            with self.assertRaisesRegex(MoneybirdError, "must be one of"):
                resolve_credentials("fallback_everywhere")


class CredentialModeMiddlewareTests(unittest.TestCase):
    @staticmethod
    def _app() -> object:
        async def endpoint(scope, receive, send) -> None:
            if scope["type"] == "lifespan":
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            body = b"ok"
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": [(b"content-length", b"2")],
                }
            )
            await send({"type": "http.response.body", "body": body})

        return endpoint

    def test_hosted_mode_returns_401_for_missing_or_blank_token(self) -> None:
        app = CredentialModeMiddleware(
            self._app(), CREDENTIAL_MODE_HOSTED_REQUEST_ONLY
        )
        with TestClient(app) as client:
            missing = client.post("/mcp")
            blank = client.post("/mcp", headers={"X-Moneybird-Token": "   "})

        self.assertEqual(missing.status_code, 401)
        self.assertEqual(blank.status_code, 401)
        self.assertEqual(
            missing.json()["error"], "moneybird_request_credentials_required"
        )

    def test_hosted_mode_passes_one_nonblank_token(self) -> None:
        app = CredentialModeMiddleware(
            self._app(), CREDENTIAL_MODE_HOSTED_REQUEST_ONLY
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp", headers={"X-Moneybird-Token": "tenant-token"}
            )
        self.assertEqual(response.status_code, 200)

    def test_network_single_user_rejects_tenant_switch_at_http_boundary(self) -> None:
        app = CredentialModeMiddleware(
            self._app(), CREDENTIAL_MODE_NETWORK_SINGLE_USER
        )
        with TestClient(app) as client:
            response = client.post(
                "/mcp", headers={"X-Moneybird-Administration-Id": "42"}
            )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "tenant_switch_forbidden")

    def test_edge_auth_runs_before_single_user_credential_policy(self) -> None:
        app = SharedSecretAuthMiddleware(
            CredentialModeMiddleware(
                self._app(), CREDENTIAL_MODE_NETWORK_SINGLE_USER
            ),
            token="edge-secret",
        )
        with TestClient(app) as client:
            unauthenticated = client.post(
                "/mcp", headers={"X-Moneybird-Token": "other-tenant"}
            )
            authenticated_switch = client.post(
                "/mcp",
                headers={
                    "Authorization": "Bearer edge-secret",
                    "X-Moneybird-Token": "other-tenant",
                },
            )
            authenticated_local = client.post(
                "/mcp",
                headers={"Authorization": "Bearer edge-secret"},
            )

        self.assertEqual(unauthenticated.status_code, 401)
        self.assertEqual(authenticated_switch.status_code, 400)
        self.assertEqual(authenticated_local.status_code, 200)


class MissingCredentialGuidanceTests(unittest.TestCase):
    """The advice has to match the mode the user is actually running.

    Request headers only exist in hosted mode, and ``scripts/`` is not part of
    the wheel, so a pip install cannot run anything under it.
    """

    def _message(self, mode: str) -> str:
        with (
            mock.patch.object(dependencies, "get_http_headers", return_value={}),
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(oauth, "get_connection", return_value=None),
        ):
            os.environ.pop("MONEYBIRD_ACCESS_TOKEN", None)
            with self.assertRaises(MoneybirdError) as caught:
                resolve_credentials(mode)
        return str(caught.exception)

    def test_local_advice_names_only_options_local_mode_has(self) -> None:
        message = self._message(CREDENTIAL_MODE_LOCAL)
        self.assertIn("MONEYBIRD_ACCESS_TOKEN", message)
        self.assertIn("moneybird-mcp auth login", message)
        self.assertNotIn("X-Moneybird-Token", message)
        self.assertNotIn("scripts/", message)

    def test_single_user_advice_rules_out_tenant_headers(self) -> None:
        message = self._message(CREDENTIAL_MODE_NETWORK_SINGLE_USER)
        self.assertIn("MONEYBIRD_ACCESS_TOKEN", message)
        self.assertIn("moneybird-mcp auth login", message)
        self.assertNotIn("scripts/", message)
        self.assertIn("rejected", message)

    def test_startup_probe_never_contacts_moneybird_or_writes_the_store(self) -> None:
        """The advisory check must not refresh an expired token.

        resolve_credentials() would: get_connection refreshes against
        Moneybird on a 20s timeout and persists the rotated token. Doing that
        before the server accepts its first connection lets a slow upstream
        stall startup until a client or health check declares the server dead.
        """
        expired = OAuthConnection(
            access_token="stored", refresh_token="rt", expires_in=1, obtained_at=0
        )
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(oauth, "load_connection", return_value=expired) as load,
            mock.patch.object(oauth, "get_connection") as resolve_token,
            mock.patch.object(oauth, "refresh_access_token") as refresh,
            mock.patch.object(oauth, "save_connection") as write,
        ):
            os.environ.pop("MONEYBIRD_ACCESS_TOKEN", None)
            configured = credentials_are_configured(CREDENTIAL_MODE_LOCAL)

        self.assertTrue(configured)  # an expired token is still a configured one
        load.assert_called_once()
        resolve_token.assert_not_called()
        refresh.assert_not_called()
        write.assert_not_called()

    def test_startup_probe_reports_environment_and_empty_store(self) -> None:
        with (
            mock.patch.dict(os.environ, {"MONEYBIRD_ACCESS_TOKEN": "token"}),
            mock.patch.object(oauth, "load_connection") as load,
        ):
            self.assertTrue(credentials_are_configured(CREDENTIAL_MODE_LOCAL))
        load.assert_not_called()  # the environment already answered

        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(oauth, "load_tokens", return_value=None),
        ):
            os.environ.pop("MONEYBIRD_ACCESS_TOKEN", None)
            self.assertFalse(credentials_are_configured(CREDENTIAL_MODE_LOCAL))

    def test_startup_probe_treats_an_unreadable_store_as_unconfigured(self) -> None:
        with (
            mock.patch.dict(os.environ, {}, clear=False),
            mock.patch.object(oauth, "load_tokens", side_effect=OSError("locked")),
        ):
            os.environ.pop("MONEYBIRD_ACCESS_TOKEN", None)
            self.assertFalse(credentials_are_configured(CREDENTIAL_MODE_LOCAL))

    def test_oauth_login_cli_ships_inside_the_installed_package(self) -> None:
        # The message above is only actionable if the command it names exists
        # wherever the package is installed from.
        module = importlib.import_module("moneybird_mcp.oauth_login")
        self.assertTrue(callable(module.main))


if __name__ == "__main__":
    unittest.main()
