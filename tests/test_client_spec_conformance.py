"""Every endpoint MoneybirdClient touches must exist in the vendored OpenAPI spec.

The snapshot in docs/moneybird_api_paths.json is generated from the official spec
bundled on developer.moneybird.com (see docs/moneybird_api_coverage.md for the
regeneration recipe). This test scans the client source for `_request(...)` calls,
expands the templated paths (document kinds, report names), and asserts each
(method, path) pair is a documented operation — so a typo'd or removed endpoint
fails CI instead of a live call.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_SOURCE = REPO_ROOT / "moneybird_mcp" / "client.py"
SPEC_PATH = REPO_ROOT / "docs" / "moneybird_api_paths.json"

DOCUMENT_COLLECTION_PATHS = [
    "documents/purchase_invoices",
    "documents/receipts",
    "documents/general_journal_documents",
]


def _normalize(path: str) -> str:
    """Collapse every {placeholder} so spec and client templates can be compared."""
    return re.sub(r"\{[^}]+\}", "*", path)


def _spec_operations() -> set[tuple[str, str]]:
    spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    operations: set[tuple[str, str]] = set()
    for path, methods in spec["paths"].items():
        normalized = _normalize(path.replace("/{administration_id}", ""))
        for method in methods:
            operations.add((method.upper(), normalized))
    return operations


def _client_operations() -> list[tuple[str, str, str]]:
    """(method, normalized_path, raw_snippet) for every _request/_binary_request call in client.py."""
    source = CLIENT_SOURCE.read_text(encoding="utf-8")
    calls = re.findall(
        r"_(?:binary_)?request\(\s*\"(GET|POST|PATCH|PUT|DELETE)\",\s*f?\"([^\"]+)\"",
        source,
    )
    operations: list[tuple[str, str, str]] = []
    for method, raw_path in calls:
        path = raw_path.replace("/{self.administration_id}", "").removesuffix(".json")
        expansions = [path]
        if "{config['collection_path']}" in path:
            # register_payment exists only for purchase invoices and receipts; the
            # client enforces the same restriction in register_document_payment.
            collections = (
                DOCUMENT_COLLECTION_PATHS[:2]
                if "register_payment" in path
                else DOCUMENT_COLLECTION_PATHS
            )
            expansions = [
                path.replace("{config['collection_path']}", collection)
                for collection in collections
            ]
        elif "{endpoint}" in path:
            from moneybird_mcp.config import REPORT_ENDPOINTS

            expansions = [
                path.replace("{endpoint}", endpoint)
                for endpoint in REPORT_ENDPOINTS.values()
            ]
        for expanded in expansions:
            operations.append((method, _normalize(expanded), raw_path))
    return operations


class ClientSpecConformanceTests(unittest.TestCase):
    def test_spec_snapshot_exists_and_is_fresh_enough(self) -> None:
        spec = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
        self.assertGreater(len(spec["paths"]), 200)
        self.assertIn("version", spec["info"])

    def test_every_client_endpoint_is_in_the_spec(self) -> None:
        spec_ops = _spec_operations()
        # raw_get is a caller-supplied passthrough; it has no fixed path to check.
        unknown = [
            (method, path, raw)
            for method, path, raw in _client_operations()
            if (method, path) not in spec_ops
        ]
        self.assertEqual(
            unknown,
            [],
            "client.py calls endpoints that are not in the vendored Moneybird spec "
            "(update docs/moneybird_api_paths.json or fix the path): "
            f"{unknown}",
        )

    def test_scan_actually_found_the_client_surface(self) -> None:
        operations = _client_operations()
        self.assertGreater(len(operations), 40)
        methods = {method for method, _, _ in operations}
        self.assertIn("GET", methods)
        self.assertIn("POST", methods)
        self.assertIn("PATCH", methods)
        self.assertIn("DELETE", methods)


if __name__ == "__main__":
    unittest.main()
