"""Resolve the active Moneybird credentials under an explicit deployment mode.

``local`` preserves the original request -> environment -> OAuth fallback used by
stdio clients and local scripts. ``network_single_user`` permits only the local
environment/OAuth identity and rejects request-supplied tenant headers.
``hosted_request_only`` accepts only a non-empty, gateway-injected request token
and never falls back to process-wide credentials.

The token is never logged.
"""
from __future__ import annotations

import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from .config import MoneybirdError

TOKEN_HEADER = "x-moneybird-token"
ADMIN_HEADER = "x-moneybird-administration-id"

CREDENTIAL_MODE_ENV = "MONEYBIRD_CREDENTIAL_MODE"
CREDENTIAL_MODE_LOCAL = "local"
CREDENTIAL_MODE_NETWORK_SINGLE_USER = "network_single_user"
CREDENTIAL_MODE_HOSTED_REQUEST_ONLY = "hosted_request_only"
CREDENTIAL_MODES = (
    CREDENTIAL_MODE_LOCAL,
    CREDENTIAL_MODE_NETWORK_SINGLE_USER,
    CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
)

# The administration in scope for the current request, set by get_client() once the
# (possibly auto-selected) administration is known. Lets lower layers such as the audit
# log scope themselves to the active tenant without threading the id through every call.
_active_administration_id: ContextVar[str | None] = ContextVar(
    "active_administration_id", default=None
)


def set_active_administration_id(administration_id: str | None) -> None:
    _active_administration_id.set(administration_id)


def get_active_administration_id() -> str | None:
    return _active_administration_id.get()


@dataclass(frozen=True)
class Credentials:
    """A resolved tenant identity: a Moneybird token and (optional) administration."""

    token: str
    administration_id: str | None
    source: str  # "request", "environment", or "oauth"


def get_credential_mode(explicit: str | None = None) -> str:
    """Return and validate the active credential/deployment mode."""
    mode = (
        explicit
        if explicit is not None
        else os.environ.get(CREDENTIAL_MODE_ENV, "")
    ).strip().lower() or CREDENTIAL_MODE_LOCAL
    if mode not in CREDENTIAL_MODES:
        raise MoneybirdError(
            f"{CREDENTIAL_MODE_ENV} must be one of {CREDENTIAL_MODES}, not {mode!r}."
        )
    return mode


def _header_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)


def _request_headers() -> dict[str, str]:
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:  # FastMCP not importable in this context
        return {}

    try:
        raw = get_http_headers(include_all=True)
    except Exception:
        # In strict hosted mode an empty mapping fails closed below. Local modes
        # retain their historical off-HTTP fallback behavior.
        return {}
    return {
        _header_text(key).lower(): _header_text(value)
        for key, value in raw.items()
    }


def _credentials_from_headers(headers: dict[str, str]) -> Credentials | None:
    token = (headers.get(TOKEN_HEADER) or "").strip()
    if not token:
        return None
    administration_id = (headers.get(ADMIN_HEADER) or "").strip() or None
    return Credentials(token=token, administration_id=administration_id, source="request")


def _credentials_from_environment() -> Credentials | None:
    token = os.environ.get("MONEYBIRD_ACCESS_TOKEN", "").strip()
    if not token:
        return None
    administration_id = os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip() or None
    return Credentials(token=token, administration_id=administration_id, source="environment")


def _credentials_from_oauth_store() -> Credentials | None:
    from . import oauth

    # The profile the operator selected, not an assumed "default". Reading a
    # fixed profile here while `auth login --profile NAME` writes another is how
    # a successful login ends up feeding a connection nothing ever loads.
    connection = oauth.get_connection(oauth.active_profile())
    if connection is None:
        return None
    # An explicit environment value still wins, keeping the whole configuration
    # system's "the parent process is authoritative" rule intact. The
    # administration chosen during `auth login` is the fallback, so a user who
    # completed OAuth does not additionally have to set an environment variable
    # for a token that can reach several administrations.
    administration_id = (
        os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip()
        or connection.administration_id
        or None
    )
    return Credentials(
        token=connection.access_token,
        administration_id=administration_id,
        source="oauth",
    )


def resolve_credentials(mode: str | None = None) -> Credentials:
    """Return credentials allowed by the active deployment mode.

    ``mode`` is primarily useful for focused callers and tests. Normal server
    processes set :data:`CREDENTIAL_MODE_ENV` during startup.
    """
    active_mode = get_credential_mode(mode)
    headers = _request_headers()

    if active_mode == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        credentials = _credentials_from_headers(headers)
        if credentials is None:
            raise MoneybirdError(
                "Hosted request credentials are required: send a non-empty "
                "'X-Moneybird-Token' header through the trusted gateway."
            )
        return credentials

    if active_mode == CREDENTIAL_MODE_NETWORK_SINGLE_USER:
        if TOKEN_HEADER in headers or ADMIN_HEADER in headers:
            raise MoneybirdError(
                "Per-request Moneybird tenant headers are not allowed in "
                "network_single_user mode."
            )
        credentials = _credentials_from_environment() or _credentials_from_oauth_store()
    else:
        credentials = (
            _credentials_from_headers(headers)
            or _credentials_from_environment()
            or _credentials_from_oauth_store()
        )

    if credentials is None:
        raise MoneybirdError(missing_credentials_message(active_mode))
    return credentials


# Only the modes that can actually fall back this far need a message; hosted
# request mode fails earlier, on the missing header.
_OAUTH_LOGIN_HINT = (
    "connect through Moneybird OAuth with 'moneybird-mcp auth login' (add "
    "--env-file PATH to point at a configuration file holding the "
    "application's client id and secret)"
)


def credentials_are_configured(mode: str) -> bool:
    """Report whether a credential source exists, without network I/O or writes.

    Startup diagnostics must use this instead of :func:`resolve_credentials`.
    Resolving reaches :func:`oauth.get_connection`, which refreshes an expired
    token against Moneybird on a 20-second timeout and rewrites the token store
    — before the server has accepted its first connection. A slow or unreachable
    Moneybird would then stall startup long enough for an MCP client or health
    check to give up on a server that is otherwise fine.

    An expired stored token counts as configured here: this answers "did anyone
    set credentials up?", and only the real resolution path can tell whether
    they still work.
    """
    if mode == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        return True  # every request carries its own; nothing to check at startup
    if _credentials_from_environment() is not None:
        return True

    from . import oauth

    try:
        record = oauth.load_connection(oauth.active_profile())
    except (MoneybirdError, OSError, ValueError):
        # An unreadable or malformed store is not proof of a configured
        # identity, and diagnosing it is the resolution path's job.
        return False
    return bool(record and record.access_token)


def missing_credentials_message(mode: str) -> str:
    """Explain how to supply credentials in the mode that is actually running.

    Naming request headers or a repository script to someone running a local
    stdio server sends them after options that mode cannot use: headers are
    only read in hosted request mode, and ``scripts/`` is not part of the
    installed wheel.
    """
    if mode == CREDENTIAL_MODE_NETWORK_SINGLE_USER:
        return (
            "No Moneybird credentials found for network_single_user mode. Set "
            "MONEYBIRD_ACCESS_TOKEN (and optionally MONEYBIRD_ADMINISTRATION_ID) "
            "in the server's environment, start it with --env-file PATH, or "
            f"{_OAUTH_LOGIN_HINT}.{_profile_note()} Per-request Moneybird tenant "
            "headers are rejected in this mode."
        )
    return (
        "No Moneybird credentials found. Set MONEYBIRD_ACCESS_TOKEN (and "
        "optionally MONEYBIRD_ADMINISTRATION_ID) in the environment your MCP "
        "client starts this server with, start it with --env-file PATH, or "
        f"{_OAUTH_LOGIN_HINT}.{_profile_note()} Get a personal token at "
        "https://moneybird.com/user/applications."
    )


def _profile_note() -> str:
    """Name the OAuth profile that was actually checked, when it is not the default.

    Without this, an operator who set MONEYBIRD_OAUTH_PROFILE reads "no
    credentials found" while a perfectly good connection sits under a different
    profile name, with nothing on screen connecting the two.
    """
    from .oauth_store import DEFAULT_PROFILE, PROFILE_ENV, active_profile

    profile = active_profile()
    if profile == DEFAULT_PROFILE:
        return ""
    return (
        f" Only the OAuth profile {profile!r} was checked, because {PROFILE_ENV} "
        "selects it."
    )


class CredentialModeMiddleware:
    """Enforce request-boundary rules before an HTTP request reaches a tool.

    The server installs shared-secret authentication outside this middleware,
    so network callers authenticate at the edge before any process-local
    credential can be selected.
    """

    def __init__(self, app: Any, mode: str) -> None:
        self.app = app
        self.mode = get_credential_mode(mode)

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        raw_headers = scope.get("headers") or []
        token_values = [
            value
            for name, value in raw_headers
            if name.lower() == TOKEN_HEADER.encode("ascii")
        ]
        admin_values = [
            value
            for name, value in raw_headers
            if name.lower() == ADMIN_HEADER.encode("ascii")
        ]

        if self.mode == CREDENTIAL_MODE_NETWORK_SINGLE_USER:
            if token_values or admin_values:
                await _send_json_error(send, 400, "tenant_switch_forbidden")
                return
        elif self.mode == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
            if len(token_values) != 1 or not token_values[0].decode(
                "latin-1"
            ).strip():
                status = 400 if len(token_values) > 1 else 401
                error = (
                    "duplicate_tenant_credentials"
                    if len(token_values) > 1
                    else "moneybird_request_credentials_required"
                )
                await _send_json_error(send, status, error)
                return
            if len(admin_values) > 1:
                await _send_json_error(send, 400, "duplicate_tenant_credentials")
                return

        await self.app(scope, receive, send)


async def _send_json_error(send: Any, status: int, error: str) -> None:
    body = ('{"error":"' + error + '"}').encode("ascii")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
