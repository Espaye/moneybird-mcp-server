"""Gateway demo (M1): OAuth onboarding flow and tenant-header injection."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import types
import unittest
import urllib.parse
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from starlette.testclient import TestClient

from gateway import app as gateway_app
from moneybird_mcp import oauth
from moneybird_mcp.credentials import (
    CREDENTIAL_MODE_ENV,
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
)

FAKE_APP_ENV = {
    "MONEYBIRD_OAUTH_CLIENT_ID": "client-id-123",
    "MONEYBIRD_OAUTH_CLIENT_SECRET": "client-secret-456",
}


class EchoMcpApp:
    """Stands in for the real MCP ASGI app: echoes path + received headers."""

    async def __call__(self, scope, receive, send) -> None:
        headers = {
            name.decode(): value.decode() for name, value in scope.get("headers", [])
        }
        body = json.dumps({"path": scope["path"], "headers": headers}).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})


class GatewayDemoTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        env_patch = mock.patch.dict(
            os.environ, {**FAKE_APP_ENV, "MONEYBIRD_MCP_DATA_DIR": self._tmp.name}
        )
        env_patch.start()
        self.addCleanup(env_patch.stop)
        self.app = gateway_app.build_gateway_app(mcp_app=EchoMcpApp())
        self.client = TestClient(self.app, follow_redirects=False)

    def _connect_user(self) -> str:
        """Run the full OAuth flow with mocked Moneybird; return the MCP endpoint."""
        login = self.client.get("/oauth/login")
        self.assertEqual(login.status_code, 302)
        authorize_url = urllib.parse.urlparse(login.headers["location"])
        params = dict(urllib.parse.parse_qsl(authorize_url.query))
        self.assertEqual(authorize_url.netloc, "moneybird.com")
        self.assertEqual(
            params["redirect_uri"], "http://testserver/oauth/callback"
        )
        state = params["state"]

        with (
            mock.patch.object(
                oauth,
                "exchange_authorization_code",
                return_value={"access_token": "moneybird-token-xyz"},
            ) as exchange,
            mock.patch.object(
                gateway_app.MoneybirdClient,
                "list_administrations",
                return_value=[{"id": 123, "name": "Demo Administratie"}],
            ),
        ):
            done = self.client.get(
                f"/oauth/callback?code=auth-code-1&state={state}"
            )
        self.assertEqual(done.status_code, 200)
        self.assertIn("Demo Administratie", done.text)
        exchange.assert_called_once_with(
            "auth-code-1", redirect_uri="http://testserver/oauth/callback"
        )
        # The endpoint URL is inside a <code> block; extract it.
        start = done.text.index("http://testserver/u/")
        end = done.text.index("</code>", start)
        return done.text[start:end]

    def test_full_flow_injects_tenant_headers(self) -> None:
        endpoint = self._connect_user()
        response = self.client.post(
            endpoint,
            json={"jsonrpc": "2.0"},
            # A malicious client trying to smuggle its own tenant:
            headers={"X-Moneybird-Token": "attacker-token"},
        )
        self.assertEqual(response.status_code, 200)
        echoed = response.json()
        self.assertEqual(echoed["path"], "/mcp")
        self.assertEqual(echoed["headers"]["x-moneybird-token"], "moneybird-token-xyz")
        self.assertEqual(echoed["headers"]["x-moneybird-administration-id"], "123")

    def test_unknown_key_is_rejected(self) -> None:
        response = self.client.post(
            "/u/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA/mcp", json={}
        )
        self.assertEqual(response.status_code, 401)

    def test_forged_state_is_rejected_before_code_exchange(self) -> None:
        with mock.patch.object(oauth, "exchange_authorization_code") as exchange:
            response = self.client.get("/oauth/callback?code=stolen&state=forged")
        self.assertIn("expired", response.text)
        exchange.assert_not_called()

    def test_state_is_single_use(self) -> None:
        endpoint = self._connect_user()
        self.assertTrue(endpoint)
        login = self.client.get("/oauth/login")
        state = dict(
            urllib.parse.parse_qsl(urllib.parse.urlparse(login.headers["location"]).query)
        )["state"]
        with (
            mock.patch.object(
                oauth,
                "exchange_authorization_code",
                return_value={"access_token": "t"},
            ),
            mock.patch.object(
                gateway_app.MoneybirdClient,
                "list_administrations",
                return_value=[{"id": 1, "name": "A"}],
            ),
        ):
            first = self.client.get(f"/oauth/callback?code=c&state={state}")
            replay = self.client.get(f"/oauth/callback?code=c&state={state}")
        self.assertEqual(first.status_code, 200)
        self.assertIn("expired", replay.text)

    def test_landing_page_never_shows_keys(self) -> None:
        endpoint = self._connect_user()
        key = endpoint.split("/u/")[1].split("/")[0]
        landing = self.client.get("/")
        self.assertIn("Connected users: 1", landing.text)
        self.assertNotIn(key, landing.text)

    def test_tokens_are_stored_per_user_profile(self) -> None:
        self._connect_user()
        users_file = os.path.join(self._tmp.name, gateway_app.USERS_FILENAME)
        with open(users_file, encoding="utf-8") as handle:
            users = json.load(handle)
        (record,) = users.values()
        self.assertTrue(record["profile"].startswith("gateway-"))
        self.assertEqual(
            oauth.get_access_token(profile=record["profile"]), "moneybird-token-xyz"
        )
        # The Moneybird token itself must not sit in the user-mapping file.
        with open(users_file, encoding="utf-8") as handle:
            self.assertNotIn("moneybird-token-xyz", handle.read())

    def test_gateway_forces_hosted_request_only_credentials(self) -> None:
        os.environ[CREDENTIAL_MODE_ENV] = "local"
        gateway_app.build_gateway_app(mcp_app=EchoMcpApp())
        self.assertEqual(
            os.environ[CREDENTIAL_MODE_ENV],
            CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        )

    def test_default_construction_mounts_mcp_app_and_forces_safe_mode(self) -> None:
        fake_mcp = mock.Mock()
        mounted_app = EchoMcpApp()
        fake_mcp.http_app.return_value = mounted_app
        fake_tools = types.ModuleType("moneybird_mcp.tools")
        fake_tools.mcp = fake_mcp
        os.environ[CREDENTIAL_MODE_ENV] = "local"

        with mock.patch.dict(sys.modules, {"moneybird_mcp.tools": fake_tools}):
            built = gateway_app.build_gateway_app()

        fake_mcp.http_app.assert_called_once_with(transport="http")
        self.assertIs(built.mcp_app, mounted_app)
        self.assertEqual(
            os.environ[CREDENTIAL_MODE_ENV],
            CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        )


if __name__ == "__main__":
    unittest.main()
