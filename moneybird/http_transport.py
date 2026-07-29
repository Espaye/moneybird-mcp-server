"""Shared HTTP transport for Moneybird JSON API calls.

The client carries no default authorization headers: every request supplies its
tenant token explicitly.  A single connection pool can therefore be reused
across administrations without mixing credentials.
"""
from __future__ import annotations

import atexit
import logging
import threading

import httpx

# httpx logs complete request URLs at INFO. Moneybird URLs contain record ids;
# our own telemetry emits a normalized endpoint instead.
logging.getLogger("httpx").setLevel(logging.WARNING)

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def get_shared_http_client() -> httpx.Client:
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = httpx.Client(
                    follow_redirects=False,
                    limits=httpx.Limits(
                        max_connections=30,
                        max_keepalive_connections=15,
                        keepalive_expiry=30.0,
                    ),
                )
    return _client


def close_shared_http_client() -> None:
    global _client
    with _client_lock:
        client, _client = _client, None
    if client is not None:
        client.close()


atexit.register(close_shared_http_client)
