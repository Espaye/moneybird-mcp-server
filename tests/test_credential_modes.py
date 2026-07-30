"""Credential-mode containment tests."""
from __future__ import annotations

import os
import unittest
from unittest import mock

import fastmcp.server.dependencies as dependencies
from starlette.testclient import TestClient

from moneybird import oauth
from moneybird.auth import SharedSecretAuthMiddleware
from moneybird.config import MoneybirdError
from moneybird.credentials import (
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    CREDENTIAL_MODE_NETWORK_SINGLE_USER,
    CredentialModeMiddleware,
    resolve_credentials,
)


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
            mock.patch.object(oauth, "get_access_token") as stored_oauth,
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
            mock.patch.object(oauth, "get_access_token") as stored_oauth,
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
            mock.patch.object(oauth, "get_access_token") as stored_oauth,
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
            mock.patch.object(oauth, "get_access_token") as stored_oauth,
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
                    mock.patch.object(oauth, "get_access_token") as stored_oauth,
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


if __name__ == "__main__":
    unittest.main()
