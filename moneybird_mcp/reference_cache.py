"""Short-lived in-process cache for rarely-changing Moneybird reference reads.

Ledger accounts and tax rates change a few times a year, but nearly every read
and every ``prepare_*`` resolves them: a measured ``list_ledger_accounts`` costs
~390 ms cold and 43 KB on the wire, and the administration membership revalidation
in front of every cached search costs another round trip. Both are pure repeat
reads within a single conversation, and Moneybird only allows 150 requests per
five minutes per IP, so repeating them is the dominant avoidable cost.

Three properties make this safe to keep this simple:

* **The cache key includes a digest of the access token.** Two tokens never share
  an entry even when they name the same administration, so the cache cannot widen
  what a caller may read. The digest uses a process-random salt so the stored key
  is not a verifier for the token itself.
* **It is disabled in ``hosted_request_only`` mode.** There a grant may be revoked
  out from under a live process, and that mode already refuses every other
  process-local cache. Local and single-user deployments own their own token.
* **The TTL is short and bounded**, so a stale entry self-heals without any
  invalidation path having to be complete. ``invalidate_administration`` exists
  for the one write that predictably changes reference data (creating a ledger
  account); it is an optimisation, not a correctness requirement.

Set ``MONEYBIRD_REFERENCE_CACHE_SECONDS=0`` to turn the whole thing off.
"""
from __future__ import annotations

import hashlib
import os
import secrets
import threading
import time
from typing import Any, Callable

# Reference data (ledger accounts, tax rates). Long enough to cover a whole
# conversation, short enough that an administration edit shows up on its own.
DEFAULT_REFERENCE_TTL_SECONDS = 600.0

# Token/administration membership. Deliberately much shorter: this entry is the
# revocation bound, so it trades at most this many seconds of staleness for
# removing a network round trip from every search.
DEFAULT_MEMBERSHIP_TTL_SECONDS = 60.0

REFERENCE_TTL_ENV = "MONEYBIRD_REFERENCE_CACHE_SECONDS"
MEMBERSHIP_TTL_ENV = "MONEYBIRD_MEMBERSHIP_CACHE_SECONDS"

# Bound on distinct (token, administration, resource) entries held at once. A
# long-lived multi-administration process must not accumulate them without limit.
MAX_ENTRIES = 256

_SALT = secrets.token_bytes(32)
_lock = threading.Lock()
_entries: dict[tuple[str, str, str], tuple[float, Any]] = {}


def _token_digest(token: str) -> str:
    return hashlib.sha256(_SALT + str(token).encode("utf-8")).hexdigest()


def _ttl_from_env(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return default
    return max(0.0, value)


def reference_ttl_seconds() -> float:
    return _ttl_from_env(REFERENCE_TTL_ENV, DEFAULT_REFERENCE_TTL_SECONDS)


def membership_ttl_seconds() -> float:
    return _ttl_from_env(MEMBERSHIP_TTL_ENV, DEFAULT_MEMBERSHIP_TTL_SECONDS)


def caching_enabled() -> bool:
    """False in hosted request mode, where a grant can be revoked mid-process."""
    from .credentials import (
        CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        get_credential_mode,
    )

    return get_credential_mode() != CREDENTIAL_MODE_HOSTED_REQUEST_ONLY


def cached_read(
    *,
    token: str,
    administration_id: str | None,
    resource: str,
    ttl_seconds: float,
    loader: Callable[[], Any],
) -> Any:
    """Return a cached value for this exact token/administration, or load it.

    A loader failure is never cached: the next call retries against Moneybird.
    """
    if ttl_seconds <= 0 or not caching_enabled():
        return loader()

    key = (_token_digest(token), str(administration_id or ""), resource)
    now = time.monotonic()
    with _lock:
        entry = _entries.get(key)
        if entry is not None and entry[0] > now:
            return entry[1]

    value = loader()

    with _lock:
        if len(_entries) >= MAX_ENTRIES:
            # Cheap bound: drop whatever expires soonest rather than tracking
            # access order. Entries are equivalent repeat reads, so evicting the
            # wrong one costs one extra request, never a wrong answer.
            for stale_key, _ in sorted(
                _entries.items(),
                key=lambda item: item[1][0],
            )[: max(1, len(_entries) - MAX_ENTRIES + 1)]:
                _entries.pop(stale_key, None)
        _entries[key] = (time.monotonic() + ttl_seconds, value)
    return value


def invalidate_administration(administration_id: str | None) -> None:
    """Drop every cached entry for one administration, across all tokens."""
    target = str(administration_id or "")
    with _lock:
        for key in [key for key in _entries if key[1] == target]:
            _entries.pop(key, None)


def clear() -> None:
    """Drop everything. Used by tests and by credential-mode switches."""
    with _lock:
        _entries.clear()


def cache_stats() -> dict[str, Any]:
    """Privacy-safe view for ``get_server_status``: counts and TTLs, no data."""
    with _lock:
        live = sum(1 for expiry, _ in _entries.values() if expiry > time.monotonic())
        return {
            "enabled": caching_enabled(),
            "entries": live,
            "reference_ttl_seconds": reference_ttl_seconds(),
            "membership_ttl_seconds": membership_ttl_seconds(),
        }
