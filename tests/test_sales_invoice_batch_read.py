"""get_sales_invoices_by_ids is a batching read, and nothing more than that.

One synchronization POST replaces up to a hundred fetch calls, which is the only
reason the tool exists. So the tests pin the two properties a caller relies on --
the caller's own ordering survives, and ids the provider did not return are
reported rather than silently dropped -- plus the property that keeps it generic:
records pass through untouched, with no interpretation, filtering or enrichment.

Every identifier below is synthesized.
"""
from __future__ import annotations

import unittest
from unittest import mock

from moneybird_mcp.tools import sales

ADMINISTRATION = "batch-read-admin"


class _BatchClient:
    administration_id = ADMINISTRATION

    def __init__(self, returned):
        self.returned = returned
        self.calls: list[list[str]] = []

    def fetch_sales_invoices_by_ids(self, ids):
        self.calls.append(list(ids))
        return [dict(item) for item in self.returned]


class SalesInvoiceBatchReadTests(unittest.TestCase):
    def _run(self, client, ids):
        with mock.patch.object(sales.ctx, "get_client", return_value=client):
            return sales.get_sales_invoices_by_ids(ids)

    def test_one_call_carries_the_caller_supplied_ids_unchanged(self) -> None:
        client = _BatchClient([{"id": "31"}, {"id": "17"}])
        result = self._run(client, ["17", "31"])

        self.assertEqual(client.calls, [["17", "31"]])
        self.assertEqual(result["api_calls"], 1)
        self.assertEqual(result["requested_count"], 2)

    def test_the_requested_order_survives_a_reordered_provider_response(self) -> None:
        client = _BatchClient([{"id": "31"}, {"id": "17"}, {"id": "24"}])
        result = self._run(client, ["17", "24", "31"])

        self.assertEqual([item["id"] for item in result["items"]], ["17", "24", "31"])

    def test_ids_the_provider_did_not_return_are_reported_not_dropped(self) -> None:
        client = _BatchClient([{"id": "17"}])
        result = self._run(client, ["17", "24", "31"])

        self.assertEqual([item["id"] for item in result["items"]], ["17"])
        self.assertEqual(result["missing_ids"], ["24", "31"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["requested_count"], 3)

    def test_records_pass_through_without_interpretation_or_filtering(self) -> None:
        record = {
            "id": "17",
            "state": "late",
            "invoice_date": "2026-02-11",
            "total_price_incl_tax": "1210.00",
            "total_unpaid": "1210.00",
            "payments": [{"id": "5001", "price": "0.00"}],
            "details": [{"id": "6001", "description": "Synthetic line"}],
            "version": 4,
        }
        client = _BatchClient([record])
        result = self._run(client, ["17"])

        self.assertEqual(result["items"], [record])

    def test_a_duplicate_requested_id_is_neither_deduplicated_nor_lost(self) -> None:
        client = _BatchClient([{"id": "17"}])
        result = self._run(client, ["17", "17"])

        self.assertEqual(client.calls, [["17", "17"]])
        self.assertEqual([item["id"] for item in result["items"]], ["17", "17"])
        self.assertEqual(result["missing_ids"], [])

    def test_an_empty_provider_response_reports_every_id_missing(self) -> None:
        client = _BatchClient([])
        result = self._run(client, ["17", "24"])

        self.assertEqual(result["items"], [])
        self.assertEqual(result["missing_ids"], ["17", "24"])
        self.assertEqual(result["count"], 0)

    def test_the_wire_schema_bounds_the_batch_to_one_provider_page(self) -> None:
        """Moneybird's synchronization endpoint takes at most 100 ids per call.

        The bound belongs in the published schema, not in a runtime check: a
        client that asks for 150 has to be told before the request is built.
        """
        import asyncio

        from fastmcp import Client

        from moneybird_mcp.tools._registry import mcp

        async def schema():
            async with Client(mcp) as client:
                for tool in await client.list_tools():
                    if tool.name == "get_sales_invoices_by_ids":
                        return tool.inputSchema
            return None

        found = asyncio.run(schema())
        self.assertIsNotNone(found)
        field = found["properties"]["sales_invoice_ids"]
        self.assertEqual(field["type"], "array")
        self.assertEqual(field["maxItems"], 100)
        self.assertEqual(field["minItems"], 1)


if __name__ == "__main__":
    unittest.main()
