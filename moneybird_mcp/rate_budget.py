"""Observed Moneybird rate-limit budget, per bucket.

Moneybird throttles **per IP address**, and publishes two separate budgets
(https://developer.moneybird.com/introduction):

* 150 requests per 5 minutes for the API generally;
* 50 requests per 5 minutes for everything under ``/reports/``.

That is the real ceiling on how much work a bookkeeping task can do, and it is
three times tighter for reports — where a quarter of VAT already costs one call
per month because Moneybird refuses a longer period.

This module deliberately does **not** throttle outgoing requests. A silent sleep
inside a tool call is indistinguishable to the user from a hung server, and a
silent refusal is worse. Instead it observes what Moneybird reports, so that:

* the retry path can honour ``RateLimit-Reset`` instead of guessing;
* an expensive scan can ask :func:`remaining` and stop with an honest message
  rather than spending the caller's whole budget; and
* ``get_server_status`` can show where the budget actually went.

State is in-memory, per process, and holds no tokens, paths, or record ids.
"""
from __future__ import annotations

import threading
import time
from typing import Any

# Documented Moneybird budgets, for reporting and for the "should I start this
# scan?" question. Treated as advisory: the live headers always win when present.
DOCUMENTED_LIMITS = {
    "general": {"requests": 150, "window_seconds": 300},
    "reports": {"requests": 50, "window_seconds": 300},
}

_lock = threading.Lock()
_buckets: dict[str, dict[str, Any]] = {}


def bucket_for_operation(operation: str) -> str:
    """Which documented budget an operation is billed against."""
    return "reports" if "/reports/" in f"/{str(operation).strip('/')}/" else "general"


def _coerce_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def record_response_headers(operation: str, headers: Any) -> None:
    """Absorb ``RateLimit-*`` headers from one response. Never raises.

    Moneybird's headers do **not** follow the IETF RateLimit draft, and reading
    them as if they did produces a number that looks like a request budget but is
    not one. Observed live on 2026-08-07, with ``now`` = 1786108940::

        ratelimit-limit: 150
        ratelimit-remaining: 160           <- SECONDS left in the window
        ratelimit-reset: 1786109100        <- absolute Unix epoch, not a delay
        ratelimit-requestsremaining: 143   <- the actual request count (undocumented)

    ``RateLimit-Remaining`` tracked ``reset - now`` exactly, and exceeded
    ``RateLimit-Limit``, which a request count cannot do. Moneybird's own prose
    agrees ("RateLimit-Remaining containing the *time* remaining"). So the request
    budget is read from ``RateLimit-RequestsRemaining``, and a ``remaining`` value
    above ``limit`` is discarded rather than believed — a caller that throttles on
    a seconds value would stop scanning for no reason.
    """
    try:
        getter = headers.get
    except AttributeError:
        return
    limit = _coerce_int(getter("RateLimit-Limit"))
    requests_left = _coerce_int(getter("RateLimit-RequestsRemaining"))
    if requests_left is None:
        # Only usable as a count when it is actually plausible as one.
        candidate = _coerce_int(getter("RateLimit-Remaining"))
        if candidate is not None and (limit is None or candidate <= limit):
            requests_left = candidate
    reset_at = _epoch_or_delay(getter("RateLimit-Reset"))
    if requests_left is None and limit is None and reset_at is None:
        return
    bucket = bucket_for_operation(operation)
    with _lock:
        state = _buckets.setdefault(bucket, {})
        state["observed_at"] = time.time()
        if requests_left is not None:
            state["remaining"] = requests_left
        if limit is not None:
            state["limit"] = limit
        if reset_at is not None:
            state["reset_monotonic"] = time.monotonic() + reset_at


def _epoch_or_delay(value: Any) -> float | None:
    """Seconds from now until reset, from either an epoch or a plain delay.

    Moneybird sends an absolute Unix timestamp; the same field is a delay in the
    IETF draft. Anything larger than one window is treated as absolute, and a
    result outside a sane range is discarded instead of guessed at.
    """
    seconds = _coerce_int(value)
    if seconds is None:
        return None
    longest_window = max(
        item["window_seconds"] for item in DOCUMENTED_LIMITS.values()
    )
    if seconds > longest_window:
        seconds = int(seconds - time.time())
    if seconds <= 0 or seconds > longest_window * 4:
        return None
    return float(seconds)


def remaining(operation_or_bucket: str) -> int | None:
    """Last observed remaining budget for this bucket, or None if never seen.

    Returns None once the observed window has elapsed: a stale count is not a
    budget, and a caller must not treat "unknown" as "exhausted".
    """
    bucket = (
        operation_or_bucket
        if operation_or_bucket in DOCUMENTED_LIMITS
        else bucket_for_operation(operation_or_bucket)
    )
    with _lock:
        state = _buckets.get(bucket)
        if not state or "remaining" not in state:
            return None
        deadline = state.get("reset_monotonic")
        if deadline is not None and time.monotonic() > deadline:
            return None
        return int(state["remaining"])


def reset_seconds(operation_or_bucket: str) -> float | None:
    """Seconds until this bucket's window resets, or None if unknown/elapsed."""
    bucket = (
        operation_or_bucket
        if operation_or_bucket in DOCUMENTED_LIMITS
        else bucket_for_operation(operation_or_bucket)
    )
    with _lock:
        state = _buckets.get(bucket)
        deadline = state.get("reset_monotonic") if state else None
    if deadline is None:
        return None
    left = deadline - time.monotonic()
    return left if left > 0 else None


def affordable_batches(operation_or_bucket: str, *, reserve: int = 10) -> int | None:
    """How many more requests a scan may make while leaving ``reserve`` spare.

    None means the budget is unknown, which callers must treat as "proceed" —
    Moneybird does not always send the headers, and refusing to work because a
    header was absent would be worse than occasionally hitting a documented 429.
    """
    left = remaining(operation_or_bucket)
    if left is None:
        return None
    return max(0, left - max(0, reserve))


def snapshot() -> dict[str, Any]:
    """Privacy-safe view for ``get_server_status``."""
    now_monotonic = time.monotonic()
    with _lock:
        observed = {
            bucket: {
                "remaining": state.get("remaining"),
                "limit": state.get("limit"),
                "resets_in_seconds": (
                    round(state["reset_monotonic"] - now_monotonic, 1)
                    if state.get("reset_monotonic") is not None
                    and state["reset_monotonic"] > now_monotonic
                    else None
                ),
            }
            for bucket, state in _buckets.items()
        }
    return {"documented_limits": DOCUMENTED_LIMITS, "observed": observed}


def clear() -> None:
    with _lock:
        _buckets.clear()
