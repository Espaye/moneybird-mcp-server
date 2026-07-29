from __future__ import annotations

import asyncio
import unittest

from fastmcp import FastMCP

from moneybird.tool_discovery import (
    ALWAYS_VISIBLE_TOOLS,
    configure_tool_discovery,
)


def _scratch_server() -> FastMCP:
    server = FastMCP("discovery-test")

    def stub() -> str:
        return "ok"

    for name in ALWAYS_VISIBLE_TOOLS:
        server.tool(stub, name=name)
    server.tool(
        stub,
        name="prepare_hidden_bank_workflow",
        description="Prepare a bank mutation reclassification workflow.",
    )
    return server


class ToolDiscoveryTests(unittest.TestCase):
    def test_full_mode_keeps_complete_catalogue(self) -> None:
        server = _scratch_server()
        self.assertEqual(configure_tool_discovery(server, "full"), "full")
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertIn("prepare_hidden_bank_workflow", names)

    def test_search_mode_exposes_compact_catalogue(self) -> None:
        server = _scratch_server()
        self.assertEqual(configure_tool_discovery(server, "search"), "search")
        names = {tool.name for tool in asyncio.run(server.list_tools())}
        self.assertEqual(
            names,
            {*ALWAYS_VISIBLE_TOOLS, "search_tools", "call_tool"},
        )
        self.assertNotIn("prepare_hidden_bank_workflow", names)


if __name__ == "__main__":
    unittest.main()
