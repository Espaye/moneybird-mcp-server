from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        process, when ``moneybird.tools`` is imported, and direct package
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
from moneybird.tools import mcp

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

    def test_crediting_an_invoice_at_least_surfaces_the_prepare_tool(self) -> None:
        # Known weaker case. BM25 normalises for document length, so the very
        # short create_credit_invoice_from_approval description outranks the
        # prepare tool's longer one. It is not dangerous — that executor refuses
        # to run without an approval_id, so the cost is a wasted call rather than
        # a wrong write — but the prepare tool must stay visible next to it.
        names = self._ranked("credit an invoice")
        self.assertIn("prepare_create_credit_invoice", names[:2], names[:4])


if __name__ == "__main__":
    unittest.main()
