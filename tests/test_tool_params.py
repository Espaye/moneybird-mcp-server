"""The Literal enums in tools/_params.py must mirror the value sets in config."""
from __future__ import annotations

import asyncio
import unittest
from typing import get_args

from moneybird.config import (
    FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
    FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES,
    PAYABLE_DOCUMENT_KINDS,
    REPORT_ENDPOINTS,
)
from moneybird.tools import _params


class LiteralSyncTests(unittest.TestCase):
    def test_report_name_literal_matches_report_endpoints(self) -> None:
        self.assertEqual(set(get_args(_params.ReportName)), set(REPORT_ENDPOINTS))

    def test_link_booking_type_literal_matches_config(self) -> None:
        self.assertEqual(
            set(get_args(_params.LinkBookingType)),
            FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
        )

    def test_unlink_booking_type_literal_matches_config(self) -> None:
        self.assertEqual(
            set(get_args(_params.UnlinkBookingType)),
            FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES,
        )

    def test_payable_document_type_literal_matches_config(self) -> None:
        self.assertEqual(
            set(get_args(_params.PayableDocumentType)),
            PAYABLE_DOCUMENT_KINDS,
        )


class ToolSchemaTests(unittest.TestCase):
    """The annotations must actually surface in the generated MCP schemas."""

    def _tool_schema(self, name: str) -> dict:
        from moneybird.tools import mcp

        return asyncio.run(mcp.get_tool(name)).parameters

    def test_report_tool_exposes_enum_and_descriptions(self) -> None:
        schema = self._tool_schema("get_financial_report")["properties"]
        self.assertEqual(set(schema["report_name"]["enum"]), set(REPORT_ENDPOINTS))
        self.assertIn("this_month", schema["period"]["description"])
        self.assertEqual(schema["page"]["minimum"], 0)

    def test_register_payment_exposes_document_type_enum(self) -> None:
        schema = self._tool_schema("prepare_register_payment")["properties"]
        self.assertEqual(set(schema["document_type"]["enum"]), PAYABLE_DOCUMENT_KINDS)
        self.assertIn("YYYY-MM-DD", schema["payment_date"]["description"])

    def test_all_tools_still_register(self) -> None:
        from moneybird.tools import mcp

        tools = asyncio.run(mcp.list_tools())
        self.assertGreater(len(tools), 60)


if __name__ == "__main__":
    unittest.main()
