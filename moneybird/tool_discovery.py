"""Configure compact on-demand discovery for the large Moneybird tool catalog."""
from __future__ import annotations

import os
from typing import Any

from .config import MoneybirdError

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

        mcp.add_transform(
            BM25SearchTransform(
                max_results=8,
                always_visible=ALWAYS_VISIBLE_TOOLS,
            )
        )
    setattr(mcp, "_moneybird_tool_discovery_mode", resolved)
    return resolved
