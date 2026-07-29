"""FastMCP middleware that groups HTTP metrics by MCP tool call."""
from __future__ import annotations

import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

from .telemetry import begin_tool_trace, end_tool_trace, record_tool_call


class ToolTelemetryMiddleware(Middleware):
    async def on_call_tool(
        self,
        context: MiddlewareContext[Any],
        call_next: CallNext[Any, Any],
    ) -> Any:
        tool_name = str(getattr(context.message, "name", None) or "unknown")
        trace_id, trace_token, tool_token = begin_tool_trace(tool_name)
        started = time.perf_counter()
        status = "success"
        try:
            return await call_next(context)
        except Exception:
            status = "error"
            raise
        finally:
            record_tool_call(
                trace_id=trace_id,
                tool_name=tool_name,
                status=status,
                duration_seconds=time.perf_counter() - started,
            )
            end_tool_trace(trace_token, tool_token)
