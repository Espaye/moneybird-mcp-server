"""Runnable server entry points.

``moneybird-mcp`` (the console script installed from pyproject.toml, e.g. via
``uvx moneybird-mcp``) starts the server on **stdio**, which is what local MCP
clients such as Claude Desktop, Claude Code and Cursor spawn as a subprocess.
``python moneybird_mcp_server.py`` keeps its historical default (legacy SSE
over HTTP) for existing deployments. Both funnel through :func:`main`.

On stdio, stdout belongs to the MCP protocol: all logging goes to stderr
(Python's logging default), never print to stdout here.
"""
from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .credentials import (
    CREDENTIAL_MODE_ENV,
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
    CREDENTIAL_MODE_LOCAL,
    CREDENTIAL_MODE_NETWORK_SINGLE_USER,
    CREDENTIAL_MODES,
)

logger = logging.getLogger("moneybird_mcp")

TRANSPORTS = ("stdio", "http", "sse")
TOOL_DISCOVERY_MODES = ("full", "search")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
TRUSTED_TLS_PROXY_ENV = "MCP_TRUSTED_TLS_PROXY"


@dataclass(frozen=True)
class ServerConfig:
    transport: str
    host: str
    port: int
    auth_token: str
    tool_discovery: str
    credential_mode: str


def build_config(
    argv: list[str] | None = None,
    *,
    default_transport: str = "stdio",
) -> ServerConfig:
    """Resolve transport/host/port from CLI flags, then env, then the default.

    Raises SystemExit for configurations that must never start, including a
    network credential mode without edge authentication.
    """
    parser = argparse.ArgumentParser(
        prog="moneybird-mcp",
        description=(
            "Moneybird MCP server. Default transport is stdio (for local MCP "
            "clients); use --transport http for a network deployment. "
            "Local and single-user credentials come from "
            "MONEYBIRD_ACCESS_TOKEN / MONEYBIRD_ADMINISTRATION_ID (or a .env "
            "file, or the OAuth token store); hosted request-only mode requires "
            "gateway-injected credentials."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=None,
        help="stdio (default), http (streamable HTTP at /mcp) or sse (legacy, at /sse). "
        "Overrides MCP_TRANSPORT.",
    )
    parser.add_argument(
        "--tool-discovery",
        choices=TOOL_DISCOVERY_MODES,
        default=None,
        help=(
            "search (default: expose compact search_tools/call_tool discovery) "
            "or full (expose every Moneybird tool up front). Overrides "
            "MCP_TOOL_DISCOVERY."
        ),
    )
    parser.add_argument(
        "--credential-mode",
        choices=CREDENTIAL_MODES,
        default=None,
        help=(
            "local (stdio only), network_single_user (authenticated network "
            "server using one env/OAuth identity), or hosted_request_only "
            "(authenticated network server requiring request credentials). "
            f"Overrides {CREDENTIAL_MODE_ENV}."
        ),
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Bind address for http/sse (default: MCP_HOST or 127.0.0.1).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Port for http/sse (default: MCP_PORT or 8000).",
    )
    args = parser.parse_args(argv)

    transport = (
        args.transport
        or os.environ.get("MCP_TRANSPORT", "").strip().lower()
        or default_transport
    )
    if transport not in TRANSPORTS:
        logger.error("MCP_TRANSPORT must be one of %s, not %r.", TRANSPORTS, transport)
        raise SystemExit(1)

    host = args.host or os.environ.get("MCP_HOST", "127.0.0.1")
    port = args.port if args.port is not None else int(os.environ.get("MCP_PORT", "8000"))
    auth_token = os.environ.get("MCP_AUTH_TOKEN", "").strip()
    tool_discovery = (
        args.tool_discovery
        or os.environ.get("MCP_TOOL_DISCOVERY", "").strip().lower()
        or "search"
    )
    if tool_discovery not in TOOL_DISCOVERY_MODES:
        logger.error(
            "MCP_TOOL_DISCOVERY must be one of %s, not %r.",
            TOOL_DISCOVERY_MODES,
            tool_discovery,
        )
        raise SystemExit(1)

    credential_mode = (
        args.credential_mode
        or os.environ.get(CREDENTIAL_MODE_ENV, "").strip().lower()
        or (
            CREDENTIAL_MODE_LOCAL
            if transport == "stdio"
            else CREDENTIAL_MODE_NETWORK_SINGLE_USER
        )
    )
    if credential_mode not in CREDENTIAL_MODES:
        logger.error(
            "%s must be one of %s, not %r.",
            CREDENTIAL_MODE_ENV,
            CREDENTIAL_MODES,
            credential_mode,
        )
        raise SystemExit(1)

    if transport == "stdio" and credential_mode != CREDENTIAL_MODE_LOCAL:
        logger.error(
            "Refusing to start: stdio requires credential mode %r, not %r.",
            CREDENTIAL_MODE_LOCAL,
            credential_mode,
        )
        raise SystemExit(1)

    if transport != "stdio" and credential_mode == CREDENTIAL_MODE_LOCAL:
        logger.error(
            "Refusing to start: credential mode %r is stdio-only. Use %r for "
            "a single-user network server or %r behind a trusted gateway.",
            CREDENTIAL_MODE_LOCAL,
            CREDENTIAL_MODE_NETWORK_SINGLE_USER,
            CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        )
        raise SystemExit(1)

    # Network modes must authenticate the edge before credential resolution.
    # This applies on loopback too: otherwise a request could reach process-wide
    # credentials without proving it belongs to the configured caller.
    if transport != "stdio" and not auth_token:
        logger.error(
            "Refusing to start: %s mode requires MCP_AUTH_TOKEN so the network "
            "edge authenticates before Moneybird credentials are selected.",
            credential_mode,
        )
        raise SystemExit(1)

    if (
        transport != "stdio"
        and host not in LOOPBACK_HOSTS
        and os.environ.get(TRUSTED_TLS_PROXY_ENV, "").strip().lower() != "true"
    ):
        logger.error(
            "Refusing to bind a plaintext network listener to %r. Terminate TLS "
            "at a trusted reverse proxy and set %s=true only when that boundary "
            "is actually in place.",
            host,
            TRUSTED_TLS_PROXY_ENV,
        )
        raise SystemExit(1)

    return ServerConfig(
        transport=transport,
        host=host,
        port=port,
        auth_token=auth_token,
        tool_discovery=tool_discovery,
        credential_mode=credential_mode,
    )


def main(argv: list[str] | None = None, *, default_transport: str = "stdio") -> None:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout stays protocol-clean
    config = build_config(argv, default_transport=default_transport)
    os.environ["MONEYBIRD_TOOL_DISCOVERY"] = config.tool_discovery
    os.environ[CREDENTIAL_MODE_ENV] = config.credential_mode

    # Importing the tools package registers all tools + prompts on the mcp
    # instance; deferred past arg parsing so --help stays instant.
    from .tools import mcp

    if config.transport == "stdio":
        # Local MCP clients spawn this process with an arbitrary cwd (often the
        # app's own directory), and data_dir() falls back to cwd when unset —
        # so give server state a stable per-user home instead.
        if not os.environ.get("MONEYBIRD_MCP_DATA_DIR", "").strip():
            os.environ["MONEYBIRD_MCP_DATA_DIR"] = str(Path.home() / ".moneybird-mcp")
        mcp.run(transport="stdio")
        return

    import uvicorn
    from starlette.middleware import Middleware

    from .auth import SharedSecretAuthMiddleware
    from .credentials import CredentialModeMiddleware

    # Middleware order is significant: Starlette treats the first item as the
    # outer layer, so shared-secret authentication runs before any credential
    # mode policy or process-local fallback.
    middleware = [
        Middleware(SharedSecretAuthMiddleware, token=config.auth_token),
        Middleware(CredentialModeMiddleware, mode=config.credential_mode),
    ]
    logger.info(
        "Shared-secret auth ENABLED; credential mode is %s.",
        config.credential_mode,
    )

    app = mcp.http_app(transport=config.transport, middleware=middleware)
    endpoint = "/sse" if config.transport == "sse" else "/mcp"
    logger.info(
        "Starting Moneybird MCP server on %s:%s (%s at %s)",
        config.host,
        config.port,
        "legacy SSE" if config.transport == "sse" else "streamable HTTP",
        endpoint,
    )
    uvicorn.run(app, host=config.host, port=config.port)


if __name__ == "__main__":
    main()
