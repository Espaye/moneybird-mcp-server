"""Search hits carry enough to skip a follow-up fetch, and the index answers by contact."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird_mcp.formatting import (
    document_search_record,
    record_facets,
    sales_invoice_search_record,
    search_hit,
)
from moneybird_mcp.purchase_review import (
    _indexed_document_ids_for_contact,
    list_documents_for_contact,
)
from moneybird_mcp.sync import RECORD_SCHEMA_VERSION

INVOICE = {
    "id": "5001",
    "invoice_id": "2026-014",
    "invoice_date": "2026-06-01",
    "state": "open",
    "total_price_incl_tax": "121.00",
    "contact": {"id": "77", "company_name": "Acme Holding BV"},
    "details": [{"description": "Advies"}],
}

PURCHASE = {
    "id": "6001",
    "reference": "F-2026-9",
    "date": "2026-06-02",
    "state": "late",
    "total_price_incl_tax": "242.00",
    "contact": {"id": "88", "company_name": "Vitens NV"},
    "details": [{"description": "Water"}],
}


class FacetTests(unittest.TestCase):
    def test_facets_are_extracted_from_either_nesting(self):
        self.assertEqual(record_facets(INVOICE)["contact_id"], "77")
        self.assertEqual(record_facets({"contact_id": "99"})["contact_id"], "99")

    def test_sales_invoice_record_carries_facets(self):
        record = sales_invoice_search_record(INVOICE, "1")
        self.assertEqual(record["contact_id"], "77")
        self.assertEqual(record["amount"], "121.00")
        self.assertEqual(record["state"], "open")
        self.assertEqual(record["date"], "2026-06-01")

    def test_document_record_carries_facets(self):
        record = document_search_record("purchase_invoice", PURCHASE, "1")
        self.assertEqual(record["contact_id"], "88")
        self.assertEqual(record["amount"], "242.00")

    def test_search_hit_drops_the_match_blob_but_keeps_facets(self):
        hit = search_hit(sales_invoice_search_record(INVOICE, "1"))
        self.assertNotIn("search_text", hit)
        self.assertEqual(
            set(hit),
            {"id", "title", "url", "contact_id", "date", "amount", "state"},
        )

    def test_search_hit_omits_empty_facets(self):
        hit = search_hit({"id": "x", "title": "t", "url": "u", "amount": ""})
        self.assertNotIn("amount", hit)


class FakeClient:
    administration_id = "1"

    def __init__(self):
        self.fetched: list[list[str]] = []
        self.version_calls = 0

    def fetch_documents_by_ids(self, kind, ids):
        self.fetched.append(list(ids))
        return [dict(PURCHASE, id=item) for item in ids]

    def list_document_versions(self, kind, filter=""):
        self.version_calls += 1
        return [{"id": str(6000 + index)} for index in range(250)]


def _index(records):
    return {
        "administration_id": "1",
        "record_schema_version": RECORD_SCHEMA_VERSION,
        "purchase_invoices": {"records": records},
    }


class IndexedContactLookupTests(unittest.TestCase):
    def test_index_names_the_exact_ids_to_fetch(self):
        index = _index(
            {
                "a": {"id": "purchase_invoice:6001", "contact_id": "88", "date": "2026-06-02"},
                "b": {"id": "purchase_invoice:6002", "contact_id": "99", "date": "2026-06-03"},
                "c": {"id": "purchase_invoice:6003", "contact_id": "88", "date": "2026-07-01"},
            }
        )
        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=index):
            ids = _indexed_document_ids_for_contact(
                FakeClient(), "purchase_invoice", contact_id="88"
            )
        self.assertEqual(ids, ["6003", "6001"])  # newest first

    def test_lookup_uses_one_batch_instead_of_scanning_everything(self):
        index = _index(
            {
                "a": {"id": "purchase_invoice:6001", "contact_id": "88", "date": "2026-06-02"},
            }
        )
        client = FakeClient()
        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=index):
            documents, meta = list_documents_for_contact(
                client, "purchase_invoice", contact_id="88"
            )
        self.assertEqual(meta["history_source"], "sync_index")
        self.assertEqual(client.version_calls, 0)
        self.assertEqual(client.fetched, [["6001"]])
        self.assertEqual([doc["id"] for doc in documents], ["6001"])

    def test_a_contact_absent_from_the_index_falls_back_to_scanning(self):
        # None, not [] — an empty list would wrongly assert "this supplier has
        # no history" when the index is simply out of date.
        index = _index(
            {"a": {"id": "purchase_invoice:6001", "contact_id": "88", "date": "2026-06-02"}}
        )
        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=index):
            self.assertIsNone(
                _indexed_document_ids_for_contact(
                    FakeClient(), "purchase_invoice", contact_id="12345"
                )
            )

    def test_stale_record_schema_is_not_trusted(self):
        index = _index(
            {"a": {"id": "purchase_invoice:6001", "contact_id": "88", "date": "2026-06-02"}}
        )
        index["record_schema_version"] = RECORD_SCHEMA_VERSION - 1
        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=index):
            self.assertIsNone(
                _indexed_document_ids_for_contact(
                    FakeClient(), "purchase_invoice", contact_id="88"
                )
            )

    def test_other_administration_index_is_never_used(self):
        index = _index(
            {"a": {"id": "purchase_invoice:6001", "contact_id": "88", "date": "2026-06-02"}}
        )
        index["administration_id"] = "999"
        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=index):
            self.assertIsNone(
                _indexed_document_ids_for_contact(
                    FakeClient(), "purchase_invoice", contact_id="88"
                )
            )

    def test_index_results_are_refiltered_against_live_records(self):
        # The index is a snapshot: a document reassigned to another contact since
        # the last sync must not come back as this supplier's history.
        index = _index(
            {"a": {"id": "purchase_invoice:6001", "contact_id": "88", "date": "2026-06-02"}}
        )

        class Reassigned(FakeClient):
            def fetch_documents_by_ids(self, kind, ids):
                return [dict(PURCHASE, id=item, contact={"id": "99"}) for item in ids]

        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=index):
            documents, _ = list_documents_for_contact(
                Reassigned(), "purchase_invoice", contact_id="88"
            )
        self.assertEqual(documents, [])

    def test_scan_fallback_reads_newest_ids_first(self):
        client = FakeClient()
        with mock.patch("moneybird_mcp.sync.load_sync_index", return_value=_index({})):
            list_documents_for_contact(
                client, "purchase_invoice", contact_id="88", limit=1
            )
        self.assertEqual(client.version_calls, 1)
        # 250 ids reversed: the first batch must be the newest hundred.
        self.assertEqual(client.fetched[0][0], "6249")


if __name__ == "__main__":
    unittest.main()
