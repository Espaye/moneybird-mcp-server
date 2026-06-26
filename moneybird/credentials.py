"""Resolve the active Moneybird credentials per request (multi-tenant), with env fallback.

Resolution priority:

1. **Per-request HTTP headers** ``X-Moneybird-Token`` and (optional)
   ``X-Moneybird-Administration-Id``. This lets one running server serve many
   administrations: every caller sends their own Moneybird token, which is the
   tenant boundary. Send these only over TLS (the cloudflared tunnel provides it).
2. **Environment** ``MONEYBIRD_ACCESS_TOKEN`` / ``MONEYBIRD_ADMINISTRATION_ID``.
   This preserves the original single-user / local behavior unchanged, and is what
   direct (non-HTTP) calls such as scripts and tests use.

The token is never logged. ``get_http_headers`` never raises and returns ``{}`` when
there is no active HTTP request, so the environment path is used automatically off-HTTP.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .config import MoneybirdError

TOKEN_HEADER = "x-moneybird-token"
ADMIN_HEADER = "x-moneybird-administration-id"


@dataclass(frozen=True)
class Credentials:
    """A resolved tenant identity: a Moneybird token and (optional) administration."""

    token: str
    administration_id: str | None
    source: str  # "request" or "environment"


def _credentials_from_headers() -> Credentials | None:
    try:
        from fastmcp.server.dependencies import get_http_headers
    except Exception:  # FastMCP not importable in this context
        return None

    raw = get_http_headers(include_all=True)  # never raises; {} outside an HTTP request
    headers = {str(key).lower(): value for key, value in raw.items()}
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


def resolve_credentials() -> Credentials:
    """Return the active tenant credentials, preferring per-request headers over env."""
    credentials = _credentials_from_headers() or _credentials_from_environment()
    if credentials is None:
        raise MoneybirdError(
            "No Moneybird credentials found. Send an 'X-Moneybird-Token' header "
            "(multi-tenant), optionally with 'X-Moneybird-Administration-Id', or set "
            "MONEYBIRD_ACCESS_TOKEN in the environment for single-user / local use."
        )
    return credentials
