"""Exercise the shipped entrypoint over real MCP transports without credentials."""
from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import (
    SSETransport,
    StdioTransport,
    StreamableHttpTransport,
)

ROOT = Path(__file__).resolve().parents[1]


async def _exercise(transport: object, credential_mode: str) -> None:
    async with Client(transport, timeout=20) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"list_contacts", "get_bookkeeping_guide", "get_server_status"} <= names
        guide = await client.call_tool("get_bookkeeping_guide", {"topic": "btw"})
        assert not guide.is_error
        assert guide.content
        assert "btw" in str(guide.content).lower()
        status = await client.call_tool("get_server_status")
        assert not status.is_error
        state = status.structured_content["credential_state"]
        assert state["mode"] == credential_mode
        assert state["configured"] is (credential_mode == "hosted_request_only")
        if credential_mode != "hosted_request_only":
            refused = await client.call_tool("list_contacts", {"limit": 1}, raise_on_error=False)
            assert refused.is_error
            assert "credentials" in str(refused.content).lower()


@pytest.mark.parametrize("transport_name,credential_mode", [
    ("stdio", "local"),
    ("http", "network_single_user"),
    ("sse", "network_single_user"),
    ("http", "hosted_request_only"),
    ("sse", "hosted_request_only"),
])
def test_real_server_transports(
    transport_name: str, credential_mode: str, tmp_path: Path,
) -> None:
    # Do not inherit developer credentials, extension paths, or the working .env.
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("MONEYBIRD_", "MCP_", "FASTMCP_", "PYTHONPATH"))
    }
    environment.update({
        "PYTHONPATH": str(ROOT),
        "PYTHONIOENCODING": "utf-8",
        "MONEYBIRD_MCP_DATA_DIR": str(tmp_path / "state"),
        "MONEYBIRD_TOOL_DISCOVERY": "full",
        "MONEYBIRD_CAPABILITY_MODE": "read_only",
    })
    arguments = [
        "-m", "moneybird_mcp", "--transport", transport_name,
        "--credential-mode", credential_mode,
    ]
    if transport_name == "stdio":
        transport = StdioTransport(
            command=sys.executable, args=arguments, env=environment, cwd=str(tmp_path),
        )
        asyncio.run(_exercise(transport, credential_mode))
        return

    environment["MCP_AUTH_TOKEN"] = "synthetic-transport-test-secret"
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    arguments += ["--host", "127.0.0.1", "--port", str(port)]
    endpoint = "sse" if transport_name == "sse" else "mcp"
    url = f"http://127.0.0.1:{port}/{endpoint}"
    with (tmp_path / "server.log").open("w+", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, *arguments], cwd=tmp_path, env=environment,
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        )
        try:
            with httpx.Client(timeout=1, trust_env=False) as http:
                deadline = time.monotonic() + 40
                while True:
                    assert process.poll() is None, "MCP server exited during startup"
                    try:
                        response = http.get(url)
                        break
                    except (httpx.ConnectError, httpx.ConnectTimeout):
                        if time.monotonic() >= deadline:
                            raise AssertionError("MCP server did not start") from None
                        time.sleep(0.1)
                assert response.status_code == 401
                assert http.get(url, headers={"X-MCP-Token": "wrong"}).status_code == 401
                headers = {"X-MCP-Token": environment["MCP_AUTH_TOKEN"]}
                tenant_headers = {"X-Moneybird-Token": "synthetic-tenant-token"}
                if credential_mode == "hosted_request_only":
                    denied = http.get(url, headers=headers)
                    assert denied.status_code == 401
                    assert denied.json()["error"] == "moneybird_request_credentials_required"
                    headers.update(tenant_headers)
                else:
                    denied = http.get(url, headers={**headers, **tenant_headers})
                    assert denied.status_code == 400
                    assert denied.json()["error"] == "tenant_switch_forbidden"

            cls = SSETransport if transport_name == "sse" else StreamableHttpTransport
            transport = cls(url, headers=headers)
            asyncio.run(_exercise(transport, credential_mode))
        except BaseException:
            log.seek(0)
            print(log.read())
            raise
        finally:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
