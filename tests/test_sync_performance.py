from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from unittest import mock

from moneybird_mcp.sync import load_sync_index, sync_search_index_data


class ParallelSyncTests(unittest.TestCase):
    class FakeClient:
        administration_id = "parallel-admin"

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._active = 0
            self.max_active = 0

        def require_current_administration_access(self):
            return {"id": self.administration_id}

        def _versions(self):
            with self._lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
            try:
                time.sleep(0.03)
                return []
            finally:
                with self._lock:
                    self._active -= 1

        def list_contact_versions(self):
            return self._versions()

        def list_sales_invoice_versions(self, *, filter=""):
            return self._versions()

        def list_document_versions(self, kind, *, filter=""):
            return self._versions()

        def list_financial_mutation_versions(self, *, filter=""):
            return self._versions()

        def fetch_contacts_by_ids(self, ids):
            return []

        def get_contact(self, item_id):
            raise AssertionError("no records should be fetched")

        def fetch_sales_invoices_by_ids(self, ids):
            return []

        def get_sales_invoice(self, item_id):
            raise AssertionError("no records should be fetched")

        def fetch_documents_by_ids(self, kind, ids):
            return []

        def get_document(self, kind, item_id):
            raise AssertionError("no records should be fetched")

        def fetch_financial_mutations_by_ids(self, ids):
            return []

        def get_financial_mutation(self, item_id):
            raise AssertionError("no records should be fetched")

    def setUp(self) -> None:
        self._data_dir = tempfile.mkdtemp(prefix="moneybird_sync_parallel_")
        self._env = mock.patch.dict(
            os.environ,
            {"MONEYBIRD_MCP_DATA_DIR": self._data_dir},
        )
        self._env.start()
        self.addCleanup(self._env.stop)

    def test_independent_version_feeds_run_with_bounded_parallelism(self) -> None:
        client = self.FakeClient()
        first = sync_search_index_data(client, force_full=True)
        first_content_timestamp = first["content_updated_at"]
        self.assertGreaterEqual(client.max_active, 2)
        self.assertLessEqual(client.max_active, 3)

        second = sync_search_index_data(client, force_full=False)
        self.assertEqual(second["content_updated_at"], first_content_timestamp)
        stored = load_sync_index(client.administration_id)
        self.assertEqual(stored["content_updated_at"], first_content_timestamp)


if __name__ == "__main__":
    unittest.main()
