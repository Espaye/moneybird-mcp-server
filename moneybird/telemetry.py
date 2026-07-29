"""Privacy-safe in-process performance telemetry.

The bookkeeping payload is deliberately never recorded.  Metrics contain only a
trace id, the MCP tool name, a normalized Moneybird endpoint, HTTP method/status,
duration, and retry number.  The bounded in-memory buffers make this useful for
diagnostics without turning the MCP server into a second bookkeeping datastore.
"""
from __future__ import annotations

import hashlib
import logging
import re
import secrets
import threading
import time
from collections import Counter, deque
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from statistics import median
from typing import Any

logger = logging.getLogger("moneybird_mcp.performance")

_MAX_API_EVENTS = 2_000
_MAX_TOOL_EVENTS = 500
_ID_SEGMENT = re.compile(r"(?<=/)\d+(?=/|\.|$)")

_current_trace_id: ContextVar[str | None] = ContextVar(
    "moneybird_trace_id",
    default=None,
)
_current_tool_name: ContextVar[str | None] = ContextVar(
    "moneybird_tool_name",
    default=None,
)
_current_tenant_scope: ContextVar[str] = ContextVar(
    "moneybird_tenant_scope",
    default="unscoped",
)


@dataclass(frozen=True)
class ApiMetric:
    timestamp: float
    trace_id: str
    tool_name: str
    method: str
    endpoint: str
    status: str
    duration_ms: float
    retry: int
    tenant_scope: str


@dataclass(frozen=True)
class ToolMetric:
    timestamp: float
    trace_id: str
    tool_name: str
    status: str
    duration_ms: float
    api_calls: int
    api_duration_ms: float
    tenant_scope: str


_api_events: deque[ApiMetric] = deque(maxlen=_MAX_API_EVENTS)
_tool_events: deque[ToolMetric] = deque(maxlen=_MAX_TOOL_EVENTS)
_metrics_lock = threading.Lock()


def normalize_endpoint(path: str) -> str:
    """Remove Moneybird/administration record ids while retaining route shape."""
    cleaned = str(path).split("?", 1)[0]
    return _ID_SEGMENT.sub(":id", cleaned)


def begin_tool_trace(tool_name: str) -> tuple[str, Token[Any], Token[Any]]:
    # Never inherit a previous request's tenant attribution. MoneybirdClient
    # establishes the current scope as soon as this tool resolves credentials.
    _current_tenant_scope.set("unscoped")
    trace_id = secrets.token_hex(8)
    trace_token = _current_trace_id.set(trace_id)
    tool_token = _current_tool_name.set(str(tool_name or "unknown"))
    return trace_id, trace_token, tool_token


def end_tool_trace(trace_token: Token[Any], tool_token: Token[Any]) -> None:
    _current_tool_name.reset(tool_token)
    _current_trace_id.reset(trace_token)


def current_trace_id() -> str:
    trace_id = _current_trace_id.get()
    if trace_id:
        return trace_id
    trace_id = secrets.token_hex(8)
    _current_trace_id.set(trace_id)
    return trace_id


def current_tool_name() -> str:
    return _current_tool_name.get() or "direct"


def tenant_scope_for_token(token: str) -> str:
    """Derive an opaque process-local grouping key without retaining the token."""
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()[:24]


def set_current_tenant_scope(scope: str) -> None:
    _current_tenant_scope.set(str(scope or "unscoped"))


def current_tenant_scope() -> str:
    return _current_tenant_scope.get()


def record_api_call(
    *,
    method: str,
    path: str,
    status: str | int,
    duration_seconds: float,
    retry: int,
    trace_id: str | None = None,
    tool_name: str | None = None,
    tenant_scope: str | None = None,
) -> None:
    metric = ApiMetric(
        timestamp=time.time(),
        trace_id=trace_id or current_trace_id(),
        tool_name=tool_name or current_tool_name(),
        method=str(method).upper(),
        endpoint=normalize_endpoint(path),
        status=str(status),
        duration_ms=round(max(0.0, duration_seconds) * 1_000, 3),
        retry=max(0, int(retry)),
        tenant_scope=tenant_scope or current_tenant_scope(),
    )
    with _metrics_lock:
        _api_events.append(metric)
    logger.info(
        "moneybird_api trace=%s tool=%s method=%s endpoint=%s status=%s "
        "duration_ms=%.3f retry=%s",
        metric.trace_id,
        metric.tool_name,
        metric.method,
        metric.endpoint,
        metric.status,
        metric.duration_ms,
        metric.retry,
    )


def _api_summary_for_trace(trace_id: str) -> tuple[int, float]:
    with _metrics_lock:
        matching = [event for event in _api_events if event.trace_id == trace_id]
    return len(matching), round(sum(event.duration_ms for event in matching), 3)


def record_tool_call(
    *,
    trace_id: str,
    tool_name: str,
    status: str,
    duration_seconds: float,
) -> None:
    api_calls, api_duration_ms = _api_summary_for_trace(trace_id)
    metric = ToolMetric(
        timestamp=time.time(),
        trace_id=trace_id,
        tool_name=str(tool_name or "unknown"),
        status=str(status),
        duration_ms=round(max(0.0, duration_seconds) * 1_000, 3),
        api_calls=api_calls,
        api_duration_ms=api_duration_ms,
        tenant_scope=current_tenant_scope(),
    )
    with _metrics_lock:
        _tool_events.append(metric)
    logger.info(
        "mcp_tool trace=%s tool=%s status=%s duration_ms=%.3f "
        "api_calls=%s api_duration_ms=%.3f",
        metric.trace_id,
        metric.tool_name,
        metric.status,
        metric.duration_ms,
        metric.api_calls,
        metric.api_duration_ms,
    )


def _percentile(values: list[float], percentage: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(
        len(ordered) - 1,
        max(0, int(round((len(ordered) - 1) * percentage))),
    )
    return round(ordered[index], 3)


def performance_snapshot(
    *,
    recent_tools: int = 20,
    tenant_scope: str | None = None,
) -> dict[str, Any]:
    """Return bounded aggregate diagnostics without payloads or credentials."""
    capped = max(1, min(int(recent_tools), 100))
    requested_scope = tenant_scope or current_tenant_scope()
    with _metrics_lock:
        api_events = [
            event
            for event in _api_events
            if event.tenant_scope == requested_scope
        ]
        tool_events = [
            event
            for event in _tool_events
            if event.tenant_scope == requested_scope
        ][-capped:]
    api_durations = [event.duration_ms for event in api_events]
    endpoints = Counter(
        f"{event.method} {event.endpoint}" for event in api_events
    )
    return {
        "privacy": (
            "Only normalized endpoints and timings are retained in memory; "
            "tokens, query values, request bodies, and response bodies are excluded."
        ),
        "api": {
            "retained_calls": len(api_events),
            "median_ms": round(median(api_durations), 3) if api_durations else 0.0,
            "p95_ms": _percentile(api_durations, 0.95),
            "top_endpoints": [
                {"endpoint": endpoint, "calls": count}
                for endpoint, count in endpoints.most_common(10)
            ],
        },
        "recent_tools": [
            {
                key: value
                for key, value in asdict(event).items()
                if key != "tenant_scope"
            }
            for event in tool_events
        ],
    }


def clear_performance_metrics() -> None:
    """Test helper; production callers normally keep the bounded rolling window."""
    with _metrics_lock:
        _api_events.clear()
        _tool_events.clear()
