"""Tests for the FTS5 search layer derived from the sync index."""
from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird import search_fts
from moneybird.sync import ensure_sync_index_shape

ADMIN = "999"


def _index(updated_at: str = "2026-07-11T12:00:00+00:00") -> dict:
    index = ensure_sync_index_shape({"administration_id": ADMIN})
    index["updated_at"] = updated_at
    index["contacts"]["records"] = {
        "1": {
            "id": "contact:1",
            "title": "Vitens N.V.",
            "url": "https://example/contacts/1",
            "search_text": "vitens n.v. water leverancier zwolle",
        },
        "2": {
            "id": "contact:2",
            "title": "KPN B.V.",
            "url": "https://example/contacts/2",
            "search_text": "kpn b.v. telecom internet den haag",
        },
    }
    index["purchase_invoices"]["records"] = {
        "10": {
            "id": "purchase_invoice:10",
            "title": "Vitens factuur juni",
            "url": "https://example/documents/10",
            "search_text": "vitens factuur juni water voorschot 12,34",
        }
    }
    return index


class FtsSearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self._data_dir = tempfile.mkdtemp(prefix="moneybird_fts_test_")
        patcher = mock.patch.dict(
            os.environ, {"MONEYBIRD_MCP_DATA_DIR": self._data_dir}
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_multi_word_out_of_order_query_matches(self) -> None:
        self.assertTrue(search_fts.refresh_fts_index(_index(), ADMIN))
        results = search_fts.search_fts(ADMIN, "water vitens", limit=5)
        self.assertEqual(
            {item["id"] for item in results},
            {"contact:1", "purchase_invoice:10"},
        )

    def test_prefix_matching_and_ranking(self) -> None:
        search_fts.refresh_fts_index(_index(), ADMIN)
        results = search_fts.search_fts(ADMIN, "vit", limit=5)
        self.assertEqual(len(results), 2)
        results = search_fts.search_fts(ADMIN, "telecom", limit=5)
        self.assertEqual([item["id"] for item in results], ["contact:2"])

    def test_or_fallback_when_not_all_words_match(self) -> None:
        search_fts.refresh_fts_index(_index(), ADMIN)
        results = search_fts.search_fts(ADMIN, "vitens bestaatnietxyz", limit=5)
        self.assertEqual(
            {item["id"] for item in results},
            {"contact:1", "purchase_invoice:10"},
        )

    def test_no_match_returns_empty_list_not_none(self) -> None:
        search_fts.refresh_fts_index(_index(), ADMIN)
        self.assertEqual(search_fts.search_fts(ADMIN, "bestaatniet", limit=5), [])

    def test_blank_query_returns_none_for_fallback(self) -> None:
        search_fts.refresh_fts_index(_index(), ADMIN)
        self.assertIsNone(search_fts.search_fts(ADMIN, "   ", limit=5))

    def test_rebuild_only_when_sync_index_changes(self) -> None:
        index = _index()
        search_fts.refresh_fts_index(index, ADMIN)

        # Same updated_at: the stale FTS content must be left alone (no rebuild).
        index["contacts"]["records"]["3"] = {
            "id": "contact:3",
            "title": "Nieuw",
            "url": "https://example/contacts/3",
            "search_text": "gloednieuw contact",
        }
        search_fts.refresh_fts_index(index, ADMIN)
        self.assertEqual(search_fts.search_fts(ADMIN, "gloednieuw", limit=5), [])

        # New updated_at: rebuild picks up the record.
        index["updated_at"] = "2026-07-11T13:00:00+00:00"
        search_fts.refresh_fts_index(index, ADMIN)
        results = search_fts.search_fts(ADMIN, "gloednieuw", limit=5)
        self.assertEqual([item["id"] for item in results], ["contact:3"])

    def test_freshness_timestamp_does_not_force_content_rebuild(self) -> None:
        index = _index()
        index["content_updated_at"] = "2026-07-11T12:00:00+00:00"
        search_fts.refresh_fts_index(index, ADMIN)
        index["contacts"]["records"]["3"] = {
            "id": "contact:3",
            "title": "Nieuw",
            "url": "https://example/contacts/3",
            "search_text": "alleen na inhoudswijziging",
        }
        index["updated_at"] = "2026-07-11T13:00:00+00:00"
        search_fts.refresh_fts_index(index, ADMIN)
        self.assertEqual(
            search_fts.search_fts(ADMIN, "inhoudswijziging", limit=5),
            [],
        )

        index["content_updated_at"] = "2026-07-11T13:00:00+00:00"
        search_fts.refresh_fts_index(index, ADMIN)
        self.assertEqual(
            [
                item["id"]
                for item in search_fts.search_fts(
                    ADMIN,
                    "inhoudswijziging",
                    limit=5,
                )
            ],
            ["contact:3"],
        )

    def test_special_characters_in_query_do_not_break_match_syntax(self) -> None:
        search_fts.refresh_fts_index(_index(), ADMIN)
        for query in ('vitens "water"', "vitens*", "vitens:water", "12,34"):
            results = search_fts.search_fts(ADMIN, query, limit=5)
            self.assertIsInstance(results, list, msg=f"query {query!r}")


if __name__ == "__main__":
    unittest.main()
