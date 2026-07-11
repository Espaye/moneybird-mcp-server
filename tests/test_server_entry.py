"""Tests for the console entry point configuration (moneybird.server)."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird.server import TRANSPORTS, build_config


def _clean_environ() -> dict[str, str]:
    """os.environ without any MCP_* server settings leaking in from .env."""
    return {
        key: value
        for key, value in os.environ.items()
        if key not in {"MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "MCP_AUTH_TOKEN"}
    }


class BuildConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.dict(os.environ, _clean_environ(), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_console_script_defaults_to_stdio(self) -> None:
        config = build_config([])
        self.assertEqual(config.transport, "stdio")

    def test_legacy_entrypoint_default_is_preserved(self) -> None:
        config = build_config([], default_transport="sse")
        self.assertEqual(config.transport, "sse")
        self.assertEqual(config.host, "127.0.0.1")
        self.assertEqual(config.port, 8000)

    def test_env_transport_beats_default(self) -> None:
        os.environ["MCP_TRANSPORT"] = "http"
        self.assertEqual(build_config([]).transport, "http")

    def test_flag_beats_env(self) -> None:
        os.environ["MCP_TRANSPORT"] = "sse"
        config = build_config(["--transport", "http"])
        self.assertEqual(config.transport, "http")

    def test_invalid_env_transport_refuses_to_start(self) -> None:
        os.environ["MCP_TRANSPORT"] = "websocket"
        with self.assertRaises(SystemExit):
            build_config([])

    def test_host_and_port_flags_beat_env(self) -> None:
        os.environ["MCP_HOST"] = "127.0.0.1"
        os.environ["MCP_PORT"] = "9999"
        config = build_config(
            ["--transport", "http", "--host", "localhost", "--port", "8123"]
        )
        self.assertEqual((config.host, config.port), ("localhost", 8123))

    def test_network_transport_refuses_non_loopback_without_auth_token(self) -> None:
        with self.assertRaises(SystemExit):
            build_config(["--transport", "http", "--host", "0.0.0.0"])

    def test_network_transport_allows_non_loopback_with_auth_token(self) -> None:
        os.environ["MCP_AUTH_TOKEN"] = "sekrit"
        config = build_config(["--transport", "http", "--host", "0.0.0.0"])
        self.assertEqual(config.auth_token, "sekrit")

    def test_stdio_ignores_the_loopback_rule(self) -> None:
        os.environ["MCP_HOST"] = "0.0.0.0"  # irrelevant for stdio: no listener
        self.assertEqual(build_config([]).transport, "stdio")

    def test_transports_constant_matches_argparse_choices(self) -> None:
        self.assertEqual(set(TRANSPORTS), {"stdio", "http", "sse"})


if __name__ == "__main__":
    unittest.main()
