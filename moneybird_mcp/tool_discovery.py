"""Configure compact on-demand discovery for the large Moneybird tool catalog."""
from __future__ import annotations

import os
from typing import Annotated, Any

from fastmcp.exceptions import ToolError
from fastmcp.exceptions import ValidationError as FastMCPValidationError
from fastmcp.server.context import Context
from fastmcp.tools.base import Tool, ToolResult
from pydantic import ValidationError as PydanticValidationError

from .config import READ_ONLY_ANNOTATIONS, MoneybirdError

TOOL_DISCOVERY_MODES = {"full", "search"}

ALWAYS_VISIBLE_TOOLS = [
    "get_server_status",
    "list_administrations",
    "search",
    "fetch",
    "sync_search_index",
    "prepare_bookkeeping_correction_batch",
    "execute_approved_action",
]


def _compact_validation_error(
    error: FastMCPValidationError,
    *,
    tool_name: str,
) -> str:
    cause = error.__cause__
    if not isinstance(cause, PydanticValidationError):
        return f"Invalid arguments for {tool_name}."
    details = []
    for issue in cause.errors(include_url=False, include_input=False):
        location = ".".join(str(part) for part in issue.get("loc") or ())
        message = str(issue.get("msg") or "invalid value")
        details.append(f"{location or 'arguments'}: {message}")
    rendered = "; ".join(details) or "invalid arguments"
    return f"Invalid arguments for {tool_name}: {rendered}."


def configure_tool_discovery(mcp: Any, mode: str | None = None) -> str:
    resolved = str(
        mode
        or os.environ.get("MONEYBIRD_TOOL_DISCOVERY", "")
        or "full"
    ).strip().lower()
    if resolved not in TOOL_DISCOVERY_MODES:
        raise MoneybirdError(
            "MONEYBIRD_TOOL_DISCOVERY must be 'full' or 'search', "
            f"not {resolved!r}."
        )
    previous = getattr(mcp, "_moneybird_tool_discovery_mode", None)
    if previous is not None:
        if previous != resolved:
            raise MoneybirdError(
                "Tool discovery was already configured as "
                f"{previous!r}; restart the server to switch modes."
            )
        return resolved
    if resolved == "search":
        from fastmcp.server.transforms.search import BM25SearchTransform

        class ReadOnlyProxyBM25SearchTransform(BM25SearchTransform):
            """Compact discovery whose generic proxy can never dispatch a write.

            MCP annotations describe the tool that the client calls, not a
            second tool selected inside that call.  A generic ``call_tool``
            therefore cannot truthfully proxy a destructive executor: the
            client would see only the proxy's annotation and could not apply
            its destructive-tool confirmation policy.  Keep the proxy limited
            to explicitly read-only tools (including prepare-only previews),
            and require every execution to use the directly listed,
            destructive ``execute_approved_action`` tool.
            """

            @staticmethod
            def _proxy_safe(tool: Tool) -> bool:
                annotations = tool.annotations
                return bool(
                    annotations is not None
                    and annotations.readOnlyHint is True
                    and annotations.destructiveHint is not True
                )

            async def _get_visible_tools(self, ctx: Context) -> list[Tool]:
                tools = await super()._get_visible_tools(ctx)
                # Hidden action-specific executors are intentionally absent
                # from search results.  The annotated generic executor is
                # already pinned in ALWAYS_VISIBLE_TOOLS.
                return [tool for tool in tools if self._proxy_safe(tool)]

            async def get_tool(
                self,
                name: str,
                call_next: Any,
                *,
                version: Any = None,
            ) -> Tool | None:
                """Reject direct lookup of hidden mutating tools.

                Search transforms normally hide tools only from ``tools/list``;
                FastMCP still delegates a direct ``tools/call`` lookup to the
                underlying catalog.  That would let a caller bypass the
                destructive annotation by naming an action-specific executor.
                """
                tool = await super().get_tool(name, call_next, version=version)
                if tool is None:
                    return None
                if name in {
                    self._search_tool_name,
                    self._call_tool_name,
                    "execute_approved_action",
                } or self._proxy_safe(tool):
                    return tool
                return None

            def _make_call_tool(self) -> Tool:
                transform = self

                async def call_tool(
                    name: Annotated[str, "The name of the read-only tool to call"],
                    arguments: Annotated[
                        dict[str, Any] | None,
                        "Arguments to pass to the read-only tool",
                    ] = None,
                    ctx: Context = None,  # type: ignore[assignment]
                ) -> ToolResult:
                    """Call a read-only tool discovered through search_tools.

                    Mutating tools must be called directly so the MCP client
                    can see and enforce their destructive annotation.
                    """
                    if name in {
                        transform._call_tool_name,
                        transform._search_tool_name,
                    }:
                        raise ToolError(
                            f"'{name}' is a synthetic search tool and cannot be "
                            "called through the call_tool proxy."
                        )

                    catalog = await transform.get_tool_catalog(ctx)
                    target = next(
                        (tool for tool in catalog if tool.name == name),
                        None,
                    )
                    if target is None:
                        raise ToolError(f"Unknown tool {name!r}.")
                    if not transform._proxy_safe(target):
                        raise ToolError(
                            f"call_tool cannot invoke mutating or unannotated tool "
                            f"{name!r}. Use the directly exposed "
                            "execute_approved_action tool with the prepared "
                            "approval_id so the MCP client sees its destructive "
                            "annotation."
                        )
                    try:
                        return await ctx.fastmcp.call_tool(name, arguments)
                    except FastMCPValidationError as exc:
                        raise ToolError(
                            _compact_validation_error(exc, tool_name=name)
                        ) from None

                return Tool.from_function(
                    fn=call_tool,
                    name=self._call_tool_name,
                    annotations=READ_ONLY_ANNOTATIONS,
                )

        mcp.add_transform(
            ReadOnlyProxyBM25SearchTransform(
                max_results=8,
                always_visible=ALWAYS_VISIBLE_TOOLS,
            )
        )
    setattr(mcp, "_moneybird_tool_discovery_mode", resolved)
    return resolved
