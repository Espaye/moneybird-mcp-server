"""Tests for the console entry point configuration (moneybird_mcp.server)."""
from __future__ import annotations

import os
import runpy
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird_mcp.server import TRANSPORTS, build_config


def _clean_environ() -> dict[str, str]:
    """os.environ without any MCP_* server settings leaking in from .env."""
    return {
        key: value
        for key, value in os.environ.items()
        if key
        not in {
            "MCP_TRANSPORT",
            "MCP_HOST",
            "MCP_PORT",
            "MCP_AUTH_TOKEN",
            "MCP_TRUSTED_TLS_PROXY",
            "MCP_TOOL_DISCOVERY",
            "MONEYBIRD_TOOL_DISCOVERY",
            "MONEYBIRD_CREDENTIAL_MODE",
        }
    }


class BuildConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_console_script_defaults_to_stdio(self) -> None:
        config = build_config([])
        self.assertEqual(config.transport, "stdio")
        self.assertEqual(config.tool_discovery, "search")
        self.assertEqual(config.credential_mode, "local")

    def test_legacy_entrypoint_transport_default_is_preserved(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        config = build_config([], default_transport="sse")
        self.assertEqual(config.transport, "sse")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)
        self.assertEqual(config.credential_mode, "network_single_user")

    def test_legacy_network_default_requires_edge_auth(self) -> None:
        with self.assertRaises(SystemExit):
            build_config([], default_transport="sse")

    def test_env_transport_beats_default(self) -> None:
        os.environ["MCP_TRANSPORT"] = "http"
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        self.assertEqual(build_config([]).transport, "http")

    def test_flag_beats_env(self) -> None:
        os.environ["MCP_TRANSPORT"] = "sse"
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        config = build_config(["--transport", "http"])
        self.assertEqual(config.transport, "http")

    def test_tool_discovery_flag_beats_env(self) -> None:
        os.environ["MCP_TOOL_DISCOVERY"] = "full"
        config = build_config(["--tool-discovery", "search"])
        self.assertEqual(config.tool_discovery, "search")

    def test_invalid_tool_discovery_env_refuses_to_start(self) -> None:
        os.environ["MCP_TOOL_DISCOVERY"] = "everything"
        with self.assertRaises(SystemExit):
            build_config([])

    def test_invalid_env_transport_refuses_to_start(self) -> None:
        os.environ["MCP_TRANSPORT"] = "websocket"
        with self.assertRaises(SystemExit):
            build_config([])

    def test_host_and_port_flags_beat_env(self) -> None:
        os.environ["MCP_HOST"] = "127.0.0.1"
        os.environ["MCP_PORT"] = "9999"
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        config = build_config(
            ["--transport", "http", "--host", "localhost", "--port", "8123"]
        )
        self.assertEqual((config.host, config.port), ("localhost", 8123))

    def test_network_transport_refuses_non_loopback_without_auth_token(self) -> None:
        with self.assertRaises(SystemExit):
            build_config(["--transport", "http", "--host", "0.0.0.0"])

    def test_network_transport_refuses_non_loopback_without_tls_proxy_ack(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        with self.assertRaises(SystemExit):
            build_config(["--transport", "http", "--host", "0.0.0.0"])

    def test_network_transport_allows_non_loopback_with_tls_proxy_ack(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        os.environ["MCP_TRUSTED_TLS_PROXY"] = "true"
        config = build_config(["--transport", "http", "--host", "0.0.0.0"])
        self.assertEqual(config.auth_token, "sekrit")
        self.assertEqual(config.credential_mode, "network_single_user")

    def test_network_transport_refuses_loopback_without_edge_auth(self) -> None:
        with self.assertRaises(SystemExit):
            build_config(["--transport", "http", "--host", "127.0.0.1"])

    def test_network_transport_refuses_local_credential_mode(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        with self.assertRaises(SystemExit):
            build_config(["--transport", "http", "--credential-mode", "local"])

    def test_hosted_request_only_is_an_authenticated_network_mode(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        config = build_config(
            ["--transport", "http", "--credential-mode", "hosted_request_only"]
        )
        self.assertEqual(config.credential_mode, "hosted_request_only")

    def test_stdio_refuses_network_credential_modes(self) -> None:
        with self.assertRaises(SystemExit):
            build_config(["--credential-mode", "hosted_request_only"])

    def test_credential_mode_flag_beats_env(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        os.environ["MONEYBIRD_CREDENTIAL_MODE"] = "hosted_request_only"
        config = build_config(
            ["--transport", "http", "--credential-mode", "network_single_user"]
        )
        self.assertEqual(config.credential_mode, "network_single_user")

    def test_invalid_credential_mode_env_refuses_to_start(self) -> None:
        os.environ["MONEYBIRD_CREDENTIAL_MODE"] = "fallback_everywhere"
        with self.assertRaises(SystemExit):
            build_config([])

    def test_stdio_ignores_the_loopback_rule(self) -> None:
        os.environ["MCP_HOST"] = "0.0.0.0"  # irrelevant for stdio: no listener
        self.assertEqual(build_config([]).transport, "stdio")

    def test_transports_constant_matches_argparse_choices(self) -> None:
        self.assertEqual(set(TRANSPORTS), {"stdio", "http", "sse"})

    def test_network_main_installs_edge_auth_before_credential_policy(self) -> None:
        import moneybird_mcp.server as server_module
        from moneybird_mcp.auth import SharedSecretAuthMiddleware
        from moneybird_mcp.credentials import CredentialModeMiddleware

        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        fake_mcp = mock.Mock()
        fake_mcp.http_app.return_value = object()
        fake_tools = types.ModuleType("moneybird_mcp.tools")
        fake_tools.mcp = fake_mcp

        with (
            mock.patch.dict(sys.modules, {"moneybird_mcp.tools": fake_tools}),
            mock.patch("uvicorn.run") as run,
        ):
            server_module.main(["--transport", "http"])

        middleware = fake_mcp.http_app.call_args.kwargs["middleware"]
        self.assertEqual(
            [item.cls for item in middleware],
            [SharedSecretAuthMiddleware, CredentialModeMiddleware],
        )
        self.assertEqual(middleware[0].kwargs["token"], "sekrit")
        self.assertEqual(
            middleware[1].kwargs["mode"],
            "network_single_user",
        )
        run.assert_called_once()

    def test_legacy_entrypoint_defers_tool_import_to_shared_main(self) -> None:
        import moneybird_mcp.server as server_module

        entrypoint = Path(__file__).resolve().parent.parent / "moneybird_mcp_server.py"
        with (
            mock.patch.object(server_module, "main") as main,
            mock.patch.object(
                sys,
                "argv",
                [str(entrypoint), "--tool-discovery", "full"],
            ),
        ):
            runpy.run_path(str(entrypoint), run_name="__main__")

        main.assert_called_once_with(default_transport="sse")
        self.assertNotIn("MONEYBIRD_TOOL_DISCOVERY", os.environ)


if __name__ == "__main__":
    unittest.main()


class MissingCredentialAnnouncementTests(unittest.TestCase):
    """An unconfigured server has to say so where the user will actually see it.

    The MCP client shows any server that starts as connected and nobody opens
    the server log, so the notice is also prepended to the instructions every
    client hands the model at connect time.
    """

    class _Server:
        def __init__(self) -> None:
            self.instructions = "ORIGINAL INSTRUCTIONS"

    def _announce(self, mode: str, *, configured: bool) -> _Server:
        from moneybird_mcp import server as server_module

        server = self._Server()
        with mock.patch(
            "moneybird_mcp.credentials.credentials_are_configured", return_value=configured
        ):
            server_module._announce_missing_credentials(mode, server)
        return server

    def test_unconfigured_server_tells_the_model_before_the_first_question(self) -> None:
        server = self._announce("local", configured=False)
        self.assertTrue(server.instructions.startswith("\nSETUP INCOMPLETE"))
        self.assertIn("MONEYBIRD_ACCESS_TOKEN", server.instructions)
        self.assertIn("python -m moneybird_mcp.oauth_login", server.instructions)
        # The real instructions must survive: the banner is a prefix, not a
        # replacement, or a configured-later session loses every rule.
        self.assertIn("ORIGINAL INSTRUCTIONS", server.instructions)

    def test_banner_admits_it_cannot_see_a_later_fix(self) -> None:
        # Instructions are sent once at connect, so a user who configures
        # credentials mid-session must not be told to keep waiting.
        server = self._announce("local", configured=False)
        self.assertIn("just configured", server.instructions)

    def test_configured_server_instructions_are_untouched(self) -> None:
        server = self._announce("local", configured=True)
        self.assertEqual(server.instructions, "ORIGINAL INSTRUCTIONS")

    def test_hosted_mode_is_never_annotated(self) -> None:
        # Every hosted request carries its own credentials; there is nothing
        # missing to announce, and the check is skipped before it runs.
        from moneybird_mcp import server as server_module

        server = self._Server()
        with mock.patch(
            "moneybird_mcp.credentials.credentials_are_configured"
        ) as probe:
            server_module._announce_missing_credentials(
                "hosted_request_only", server
            )
        probe.assert_not_called()
        self.assertEqual(server.instructions, "ORIGINAL INSTRUCTIONS")

    def test_a_failure_to_annotate_never_stops_startup(self) -> None:
        from moneybird_mcp import server as server_module

        class _Locked:
            instructions = "X"

            def __setattr__(self, name: str, value: object) -> None:
                raise RuntimeError("read-only")

        with mock.patch(
            "moneybird_mcp.credentials.credentials_are_configured", return_value=False
        ):
            server_module._announce_missing_credentials("local", _Locked())


class ServerStatusCredentialTests(unittest.TestCase):
    def test_status_reports_missing_credentials_without_resolving_a_client(self) -> None:
        from moneybird_mcp.tools import core

        with (
            mock.patch.object(
                core,
                "credentials_are_configured",
                return_value=False,
            ),
            mock.patch.object(core.ctx, "get_client") as get_client,
            mock.patch.dict(
                os.environ,
                {"MONEYBIRD_CREDENTIAL_MODE": "local"},
            ),
        ):
            status = core.get_server_status()

        get_client.assert_not_called()
        self.assertEqual(status["version"], "0.6.0")
        self.assertEqual(
            status["credential_state"],
            {
                "mode": "local",
                "configured": False,
                "message": mock.ANY,
            },
        )
        self.assertIn("MONEYBIRD_ACCESS_TOKEN", status["credential_state"]["message"])


class PackagingVersionSyncTests(unittest.TestCase):
    """The wheel (pyproject) and the Claude Desktop bundle (mcpb manifest) must
    always release the same version number."""

    def test_pyproject_and_mcpb_manifest_versions_match(self) -> None:
        import json
        import tomllib
        from pathlib import Path

        import moneybird_mcp

        root = Path(__file__).resolve().parent.parent
        pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
        manifest = json.loads((root / "mcpb" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(pyproject["project"]["version"], manifest["version"])
        self.assertEqual(pyproject["project"]["version"], moneybird_mcp.__version__)
