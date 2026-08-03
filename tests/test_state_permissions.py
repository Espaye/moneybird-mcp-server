from __future__ import annotations

import os
import tempfile
import unittest
from unittest import mock

from moneybird_mcp import config, safety, search_fts, sync


class StatePermissionTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp_dir = tempfile.TemporaryDirectory(
            prefix="moneybird_state_permissions_"
        )
        self._env = mock.patch.dict(
            os.environ,
            {"MONEYBIRD_MCP_DATA_DIR": self._temp_dir.name},
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._temp_dir.cleanup()

    def test_dedicated_data_directory_gets_owner_only_mode(self) -> None:
        with mock.patch.object(config.os, "chmod") as chmod:
            path = config.data_dir()
        chmod.assert_called_with(path, 0o700)

    def test_approval_database_and_audit_log_are_hardened(self) -> None:
        with mock.patch.object(safety, "harden_private_file") as harden:
            with safety._approvals_connection():
                pass
            safety.append_audit_log(
                {"action": "permission-test", "result": "failed"},
                administration_id="123",
            )

        hardened_paths = [call.args[0] for call in harden.call_args_list]
        self.assertIn(safety.approvals_db_path(), hardened_paths)
        self.assertIn(safety.audit_log_path("123"), hardened_paths)

    def test_sync_and_fts_cache_files_are_hardened(self) -> None:
        with mock.patch.object(sync, "harden_private_file") as harden_sync:
            sync.save_sync_index(
                {"administration_id": "123"},
                administration_id="123",
            )
        self.assertIn(
            sync.sync_index_path("123"),
            [call.args[0] for call in harden_sync.call_args_list],
        )

        with mock.patch.object(search_fts, "harden_private_file") as harden_fts:
            connection = search_fts._connect("123")
            if connection is not None:
                connection.close()
        harden_fts.assert_called_with(search_fts.fts_index_path("123"))


if __name__ == "__main__":
    unittest.main()
