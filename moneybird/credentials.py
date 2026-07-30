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

    token = oauth.get_access_token()
    if not token:
        return None
    administration_id = os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip() or None
    return Credentials(token=token, administration_id=administration_id, source="oauth")


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
        if active_mode == CREDENTIAL_MODE_NETWORK_SINGLE_USER:
            raise MoneybirdError(
                "No Moneybird operator credentials found for network_single_user "
                "mode. Set MONEYBIRD_ACCESS_TOKEN or log in via OAuth with "
                "scripts/oauth_login.py."
            )
        raise MoneybirdError(
            "No Moneybird credentials found. Send an 'X-Moneybird-Token' header "
            "(multi-tenant), optionally with 'X-Moneybird-Administration-Id', set "
            "MONEYBIRD_ACCESS_TOKEN in the environment, or log in via OAuth with "
            "scripts/oauth_login.py."
        )
    return credentials


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
