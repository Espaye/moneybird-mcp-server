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

logger = logging.getLogger("moneybird_mcp")

TRANSPORTS = ("stdio", "http", "sse")
TOOL_DISCOVERY_MODES = ("full", "search")
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


@dataclass(frozen=True)
class ServerConfig:
    transport: str
    host: str
    port: int
    auth_token: str
    tool_discovery: str


def build_config(
    argv: list[str] | None = None,
    *,
    default_transport: str = "stdio",
) -> ServerConfig:
    """Resolve transport/host/port from CLI flags, then env, then the default.

    Raises SystemExit for configurations that must never start: an unknown
    transport, or a network transport bound beyond loopback without
    MCP_AUTH_TOKEN.
    """
    parser = argparse.ArgumentParser(
        prog="moneybird-mcp",
        description=(
            "Moneybird MCP server. Default transport is stdio (for local MCP "
            "clients); use --transport http for a network deployment. "
            "Credentials come from MONEYBIRD_ACCESS_TOKEN / "
            "MONEYBIRD_ADMINISTRATION_ID (or a .env file, or the OAuth token "
            "store)."
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

    # stdio is inherently local (the client owns both pipe ends); the loopback
    # rule only guards network transports.
    if transport != "stdio" and not auth_token and host not in LOOPBACK_HOSTS:
        logger.error(
            "Refusing to start: host=%s is non-loopback but MCP_AUTH_TOKEN is unset. "
            "Set MCP_AUTH_TOKEN to allow non-loopback binding.",
            host,
        )
        raise SystemExit(1)

    return ServerConfig(
        transport=transport,
        host=host,
        port=port,
        auth_token=auth_token,
        tool_discovery=tool_discovery,
    )


def main(argv: list[str] | None = None, *, default_transport: str = "stdio") -> None:
    logging.basicConfig(level=logging.INFO)  # stderr; stdout stays protocol-clean
    config = build_config(argv, default_transport=default_transport)
    os.environ["MONEYBIRD_TOOL_DISCOVERY"] = config.tool_discovery

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

    middleware = []
    if config.auth_token:
        middleware.append(Middleware(SharedSecretAuthMiddleware, token=config.auth_token))
        logger.info("Shared-secret auth ENABLED on the MCP endpoint.")
    else:
        logger.warning(
            "MCP_AUTH_TOKEN is not set: the MCP endpoint has NO authentication. "
            "This is only safe because host=%s. Set MCP_AUTH_TOKEN before exposing "
            "the server beyond loopback.",
            config.host,
        )

    app = mcp.http_app(transport=config.transport, middleware=middleware or None)
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
