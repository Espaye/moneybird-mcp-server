"""The Literal enums in tools/_params.py must mirror the value sets in config."""
from __future__ import annotations

import asyncio
import unittest
from typing import get_args

from moneybird_mcp.config import (
    FINANCIAL_MUTATION_UNLINK_BOOKING_TYPES,
    PAYABLE_DOCUMENT_KINDS,
    REPORT_ENDPOINTS,
    VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
)
from moneybird_mcp.tools import _params


class LiteralSyncTests(unittest.TestCase):
    def test_report_name_literal_matches_report_endpoints(self) -> None:
        self.assertEqual(set(get_args(_params.ReportName)), set(REPORT_ENDPOINTS))

    def test_link_booking_type_literal_matches_config(self) -> None:
        self.assertEqual(
            set(get_args(_params.LinkBookingType)),
            VERIFIABLE_FINANCIAL_MUTATION_LINK_BOOKING_TYPES,
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
        from moneybird_mcp.tools import mcp

        return asyncio.run(mcp.get_tool(name)).parameters

    def test_report_tool_exposes_enum_and_descriptions(self) -> None:
        schema = self._tool_schema("get_financial_report")["properties"]
        self.assertEqual(set(schema["report_name"]["enum"]), set(REPORT_ENDPOINTS))
        self.assertIn("this_month", schema["period"]["description"])
        self.assertEqual(schema["page"]["minimum"], 0)

    def test_report_periods_have_safe_defaults(self) -> None:
        profit_loss = self._tool_schema("get_profit_loss")
        balance_sheet = self._tool_schema("get_balance_sheet")
        general_ledger = self._tool_schema("get_general_ledger")
        generic = self._tool_schema("get_financial_report")

        for schema in (profit_loss, balance_sheet, general_ledger):
            self.assertNotIn("period", schema.get("required", []))
            self.assertEqual(schema["properties"]["period"]["default"], "this_year")
        self.assertNotIn("period", generic.get("required", []))
        self.assertEqual(generic["properties"]["period"]["default"], "this_month")

    def test_register_payment_exposes_document_type_enum(self) -> None:
        schema = self._tool_schema("prepare_register_payment")["properties"]
        self.assertEqual(set(schema["document_type"]["enum"]), PAYABLE_DOCUMENT_KINDS)
        self.assertIn("YYYY-MM-DD", schema["payment_date"]["description"])

    def test_bank_reclassification_exposes_guarded_batch_entries(self) -> None:
        schema = self._tool_schema(
            "prepare_reclassify_bank_mutation_bookings"
        )["properties"]
        self.assertIn("entries", schema)
        self.assertIn(
            "ledger_account_booking_id",
            schema["entries"]["description"],
        )

    def test_purchase_reconcile_exposes_exact_pdf_line_mode(self) -> None:
        schema = self._tool_schema("prepare_reconcile_purchase_invoice")["properties"]
        self.assertIn("desired_lines", schema)
        self.assertIn("actual invoice/PDF", schema["desired_lines"]["description"])
        self.assertIn("prices_are_incl_tax", schema)

    def test_create_ledger_account_requires_and_explains_rgs_code(self) -> None:
        tool_schema = self._tool_schema("prepare_create_ledger_account")
        self.assertIn("rgs_code", tool_schema["required"])
        description = tool_schema["properties"]["rgs_code"]["description"]
        self.assertIn("WBedAlkOal", description)
        self.assertIn("list_ledger_accounts", description)

    def test_purchase_review_exposes_optional_description_checks(self) -> None:
        schema = self._tool_schema("review_purchase_invoices")["properties"]
        option = schema["include_description_mapping_checks"]
        self.assertTrue(option["default"])
        self.assertIn("advisory", option["description"])

    def test_vat_analysis_exposes_its_explicit_range_contract(self) -> None:
        schema = self._tool_schema("analyze_vat_settlement")["properties"]
        self.assertIn("Explicit whole-month date range only", schema["period"]["description"])
        self.assertNotIn("rounding_ledger_account_id", schema)

    def test_direct_purchase_reference_lookup_registers(self) -> None:
        schema = self._tool_schema("get_purchase_invoice_by_reference")["properties"]
        self.assertIn("reference", schema)
        self.assertIn("Exact supplier invoice number", schema["reference"]["description"])

    def test_generic_approval_executor_registers(self) -> None:
        schema = self._tool_schema("execute_approved_action")["properties"]
        self.assertIn("approval_id", schema)

    def test_combined_bookkeeping_workflow_registers(self) -> None:
        schema = self._tool_schema(
            "prepare_bookkeeping_correction_batch"
        )["properties"]
        self.assertIn("bank_reclassifications", schema)
        self.assertIn("purchase_reconciliations", schema)

    def test_all_tools_still_register(self) -> None:
        from moneybird_mcp.tools import mcp

        tools = asyncio.run(mcp.list_tools())
        self.assertGreater(len(tools), 60)


if __name__ == "__main__":
    unittest.main()
