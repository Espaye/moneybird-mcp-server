"""An expected refusal must not be logged as a crash.

Every deliberate refusal this server makes — missing credentials, a rejected
period, a failed precondition — is a MoneybirdError. FastMCP logs an exception
type it does not recognise with ``logger.exception``, and its RichHandler
renders that as a boxed multi-frame traceback with source lines. In an MCP
client log (Claude Desktop writes stderr straight into it) that reads like the
server fell over, which sends a user who simply forgot their token looking for
a bug. FastMCP logs ``FastMCPError`` without a traceback instead, so the tool
surface translates into that category.
"""
from __future__ import annotations

import asyncio
import logging
import unittest
from unittest import mock

from fastmcp.exceptions import ToolError

from moneybird_mcp.config import MoneybirdError
from moneybird_mcp.tools import _context
from moneybird_mcp.tools import contacts as contact_tools
from moneybird_mcp.tools._registry import mcp

MESSAGE = "No Moneybird credentials found. Set MONEYBIRD_ACCESS_TOKEN ..."


class _RecordCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class ExpectedToolErrorReportingTests(unittest.TestCase):
    def _call_over_mcp(self) -> tuple[Exception, list[logging.LogRecord]]:
        """Invoke the registered tool the way a client does, capturing fastmcp logs."""
        capture = _RecordCapture()
        # Child loggers propagate to this handler, so a record emitted by
        # fastmcp.server.server is captured here regardless of its module name.
        fastmcp_logger = logging.getLogger("fastmcp")
        fastmcp_logger.addHandler(capture)
        self.addCleanup(fastmcp_logger.removeHandler, capture)

        with mock.patch.object(
            _context, "get_client", side_effect=MoneybirdError(MESSAGE)
        ):
            with self.assertRaises(Exception) as caught:  # noqa: B017 - type asserted below
                asyncio.run(mcp.call_tool("list_contacts", {"limit": 1}))
        return caught.exception, capture.records

    def test_refusal_reaches_fastmcp_as_an_expected_error(self) -> None:
        error, _ = self._call_over_mcp()
        self.assertIsInstance(error, ToolError)
        self.assertIn("MONEYBIRD_ACCESS_TOKEN", str(error))

    def test_refusal_is_not_logged_with_a_traceback(self) -> None:
        _, records = self._call_over_mcp()
        with_traceback = [record for record in records if record.exc_info]
        self.assertEqual(
            with_traceback,
            [],
            "an expected refusal was logged with exc_info, which renders as a "
            "traceback in the MCP client log",
        )

    def test_reason_is_still_written_to_the_server_log(self) -> None:
        # Suppressing the traceback must not suppress the diagnosis: FastMCP's
        # own line for an expected error names only the tool.
        with self.assertLogs("moneybird_mcp", level=logging.ERROR) as logs:
            self._call_over_mcp()
        self.assertTrue(
            any("MONEYBIRD_ACCESS_TOKEN" in line for line in logs.output),
            logs.output,
        )
        self.assertTrue(any("list_contacts" in line for line in logs.output))

    def test_direct_python_callers_still_see_moneybird_error(self) -> None:
        # Scripts, tests, and one-off integration flows import these
        # functions directly; only the MCP-facing callable is translated.
        with mock.patch.object(
            _context, "get_client", side_effect=MoneybirdError(MESSAGE)
        ):
            with self.assertRaises(MoneybirdError):
                contact_tools.list_contacts(limit=1)


if __name__ == "__main__":
    unittest.main()
