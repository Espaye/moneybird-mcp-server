from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from moneybird_mcp.config import (
    PREPARE_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
    WRITE_ANNOTATIONS,
)
from moneybird_mcp.tool_discovery import (
    ALWAYS_VISIBLE_TOOLS,
    configure_tool_discovery,
)


def _scratch_server() -> FastMCP:
    server = FastMCP("discovery-test")

    def stub() -> str:
        return "ok"

    for name in ALWAYS_VISIBLE_TOOLS:
        annotations = (
            WRITE_ANNOTATIONS
            if name == "execute_approved_action"
            else READ_ONLY_ANNOTATIONS
        )
        server.tool(stub, name=name, annotations=annotations)
    server.tool(
        stub,
        name="prepare_hidden_bank_workflow",
        description="Prepare a bank mutation reclassification workflow.",
        annotations=PREPARE_ANNOTATIONS,
    )
    server.tool(
        stub,
        name="hidden_write_from_approval",
        description="Execute a prepared mutation.",
        annotations=WRITE_ANNOTATIONS,
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
        tools = asyncio.run(server.list_tools())
        names = {tool.name for tool in tools}
        self.assertEqual(
            names,
            {*ALWAYS_VISIBLE_TOOLS, "search_tools", "call_tool"},
        )
        self.assertNotIn("prepare_hidden_bank_workflow", names)
        proxy = next(tool for tool in tools if tool.name == "call_tool")
        self.assertTrue(proxy.annotations.readOnlyHint)
        self.assertFalse(proxy.annotations.destructiveHint)

    def test_call_tool_runs_read_only_and_prepare_tools(self) -> None:
        server = _scratch_server()
        configure_tool_discovery(server, "search")

        async def call() -> str:
            async with Client(server) as client:
                result = await client.call_tool(
                    "call_tool",
                    {
                        "name": "prepare_hidden_bank_workflow",
                        "arguments": {},
                    },
                )
                return str(result.content[0].text)

        self.assertEqual(asyncio.run(call()), "ok")

    def test_call_tool_refuses_mutating_executor(self) -> None:
        server = _scratch_server()
        configure_tool_discovery(server, "search")

        async def call() -> None:
            async with Client(server) as client:
                await client.call_tool(
                    "call_tool",
                    {
                        "name": "hidden_write_from_approval",
                        "arguments": {},
                    },
                )

        with self.assertRaisesRegex(
            ToolError,
            "directly exposed execute_approved_action",
        ):
            asyncio.run(call())

    def test_direct_call_refuses_hidden_mutating_executor(self) -> None:
        server = _scratch_server()
        configure_tool_discovery(server, "search")

        async def call() -> None:
            async with Client(server) as client:
                await client.call_tool("hidden_write_from_approval", {})

        with self.assertRaisesRegex(ToolError, "Unknown tool"):
            asyncio.run(call())

    def test_direct_call_keeps_annotated_generic_executor_available(self) -> None:
        server = _scratch_server()
        configure_tool_discovery(server, "search")

        async def call() -> str:
            async with Client(server) as client:
                result = await client.call_tool("execute_approved_action", {})
                return str(result.content[0].text)

        self.assertEqual(asyncio.run(call()), "ok")

    def test_search_results_hide_action_specific_executors(self) -> None:
        server = _scratch_server()
        configure_tool_discovery(server, "search")

        async def search() -> list[str]:
            async with Client(server) as client:
                result = await client.call_tool(
                    "search_tools",
                    {"query": "prepared mutation execute"},
                )
                data = result.structured_content or {}
                entries = data.get("tools") or data.get("result") or []
                return [entry["name"] for entry in entries]

        self.assertNotIn("hidden_write_from_approval", asyncio.run(search()))


class ToolSearchRankingTests(unittest.TestCase):
    """The tool a plain request describes has to come back first.

    In compact discovery mode search_tools is how a model finds anything at all,
    so a miss here is not cosmetic: the model acts on what it is shown. BM25
    ranks on the tool name plus its description and parameter descriptions, and
    weights rare words heavily — "create a new contact" used to return
    prepare_create_credit_invoice first, because "new" is rare across the
    catalogue while "contact" appears in a dozen tool names. The fix is that
    descriptions have to carry the words users actually type.
    """

    QUERIES = {
        "create a new contact": "prepare_create_contact",
        "add contact": "prepare_create_contact",
        "new customer": "prepare_create_contact",
        "add a supplier": "prepare_create_contact",
        "change a contact's address": "prepare_update_contact",
        "send an invoice to a customer": "prepare_send_sales_invoice",
        "make a draft invoice": "prepare_create_sales_invoice_draft",
        "record a payment on an invoice": "prepare_register_payment",
        "book a bank transaction to a ledger": "prepare_link_bank_mutation_booking",
    }

    _ranking: dict[str, list[str]] | None = None

    @classmethod
    def _rankings(cls) -> dict[str, list[str]]:
        """Rank every query against a real server started in compact mode.

        Deliberately a subprocess. The discovery mode is decided once per
        process, when ``moneybird_mcp.tools`` is imported, and direct package
        imports default to ``full`` — so an in-process test would either skip or
        have to rebuild the catalogue on a scratch server. Rebuilding measured
        identically here, but this is what actually ships: the same transform,
        over the same catalogue, chosen the same way the runnable server chooses
        it. No fidelity argument to make.
        """
        if cls._ranking is not None:
            return cls._ranking

        root = Path(__file__).resolve().parent.parent
        probe = """
import asyncio, json, os, sys
from fastmcp import Client
from moneybird_mcp.tools import mcp

QUERIES = json.loads(sys.argv[1])

async def main():
    out = {}
    async with Client(mcp) as client:
        for query in QUERIES:
            result = await client.call_tool("search_tools", {"query": query})
            data = result.structured_content or {}
            entries = data.get("tools") or data.get("result") or []
            out[query] = [entry.get("name") for entry in entries]
    print(json.dumps(out))

asyncio.run(main())
"""
        environment = dict(os.environ)
        environment.update(
            {
                "MONEYBIRD_TOOL_DISCOVERY": "search",
                "MONEYBIRD_MCP_DATA_DIR": tempfile.mkdtemp(prefix="moneybird_rank_"),
                "PYTHONPATH": str(root),
                "PYTHONIOENCODING": "utf-8",
            }
        )
        queries = json.dumps(list(cls.QUERIES) + ["credit an invoice"])
        completed = subprocess.run(
            [sys.executable, "-c", probe, queries],
            cwd=root,
            env=environment,
            capture_output=True,
            # Inheriting an invalid stdin handle raises WinError 6 before the
            # probe runs; see the note in tests/test_env_file_boundary.py.
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=300,
        )
        if completed.returncode != 0:
            raise AssertionError(
                f"ranking probe failed ({completed.returncode}):\n{completed.stderr[-2000:]}"
            )
        cls._ranking = json.loads(completed.stdout.strip().splitlines()[-1])
        return cls._ranking

    def _ranked(self, query: str) -> list[str]:
        return self._rankings()[query]

    def test_plain_requests_rank_their_tool_first(self) -> None:
        for query, expected in self.QUERIES.items():
            with self.subTest(query=query):
                names = self._ranked(query)
                self.assertEqual(
                    names[:1],
                    [expected],
                    f"{query!r} ranked {names[:3]}",
                )

    def test_crediting_an_invoice_ranks_the_prepare_tool_first(self) -> None:
        # Action-specific executors are omitted from compact search results;
        # execution must use the directly exposed annotated generic executor.
        names = self._ranked("credit an invoice")
        self.assertEqual(names[:1], ["prepare_create_credit_invoice"], names[:4])
        self.assertNotIn("create_credit_invoice_from_approval", names)


if __name__ == "__main__":
    unittest.main()
