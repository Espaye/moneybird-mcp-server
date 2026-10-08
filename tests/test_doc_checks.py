"""Regression/failure tests for offline documentation checks; no provider or DB dependency."""

from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "check_docs", Path(__file__).resolve().parents[1] / "scripts/check_docs.py"
)
assert SPEC is not None and SPEC.loader is not None
CHECKER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CHECKER)


class DocumentationCheckerTests(unittest.TestCase):
    def test_heading_anchors_preserve_unicode_and_duplicate_suffixes(self) -> None:
        self.assertEqual(
            CHECKER.anchors('# Café **VAT**\n# Café VAT\n## `MCP` first\n<a id="RB-01-01"></a>'),
            {"café-vat", "café-vat-1", "mcp-first", "RB-01-01"},
        )

    def test_fenced_examples_and_inline_code_are_not_links(self) -> None:
        self.assertEqual(CHECKER.links("```md\n[example](missing.md)\n```\n`[0-9][0-9.]`\n"), [])
        self.assertEqual(CHECKER.anchors("~~~md\n# Not a heading\n~~~\n## Real"), {"real"})

    def test_reference_and_html_navigation(self) -> None:
        targets = [target for _, target in CHECKER.links('[gate][g]\n[g]: gates.md#pass\n<a href="state.md">State</a>')]
        self.assertIn("gates.md#pass", targets)
        self.assertIn("state.md", targets)
        self.assertIn("!undefined:missing", [target for _, target in CHECKER.links("[x][missing]")])

    def test_space_encoded_paths_and_titles(self) -> None:
        self.assertEqual(CHECKER.links('[a](<My File.md#one> "title")'), [(1, "My File.md#one")])
        self.assertEqual(CHECKER.links("[a](My%20File.md#one)"), [(1, "My%20File.md#one")])

    def test_missing_file_anchor_escape_and_fact_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "doc.md").write_text(
                "# Current\n[missing](gone.md)\n[anchor](doc.md#old)\n[escape](../outside.md)", encoding="utf-8"
            )
            (root / "source.json").write_text('{"count": 7}', encoding="utf-8")
            config = {
                "facts": [
                    {"source": "source.json", "pointer": "count", "document": "doc.md", "pattern": "count=(\\d+)"}
                ]
            }
            (root / "docs/checks.json").write_text(json.dumps(config), encoding="utf-8")
            with patch.object(CHECKER.subprocess, "check_output", return_value="doc.md\n"):
                errors = CHECKER.document_errors(root)
            self.assertEqual(len(errors), 4, errors)
            for category in ["missing file", "missing anchor", "outside repository", "source fact drift"]:
                self.assertTrue(any(category in error for error in errors), errors)

    def test_fact_filter_and_valid_cross_repository_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            (root / "peer").mkdir()
            (root / "peer/readme.md").write_text("# Contract", encoding="utf-8")
            (root / "doc.md").write_text(
                "eligible=1\n[peer](https://github.com/Owner/Peer/blob/main/readme.md#contract)", encoding="utf-8"
            )
            (root / "source.json").write_text('[{"registered": true}, {"registered": false}]', encoding="utf-8")
            config = {
                "repository": "Owner/Self",
                "facts": [
                    {
                        "source": "source.json",
                        "pointer": "",
                        "filter": ["registered", True],
                        "length": True,
                        "document": "doc.md",
                        "pattern": "eligible=(\\d+)",
                    }
                ],
            }
            (root / "docs/checks.json").write_text(json.dumps(config), encoding="utf-8")
            with patch.object(CHECKER.subprocess, "check_output", return_value="doc.md\n"):
                self.assertEqual(CHECKER.document_errors(root, {"Owner/Peer": root / "peer"}), [])
                (root / "peer/readme.md").write_text("# Changed", encoding="utf-8")
                self.assertTrue(
                    any(
                        "missing anchor" in error
                        for error in CHECKER.document_errors(root, {"Owner/Peer": root / "peer"})
                    )
                )

    def test_endpoint_inventory_and_coverage_totals_detect_drift(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "docs").mkdir()
            document = "> **1** dedicated\n| GET | `/contacts` | List | ✅ tool |\n"
            (root / "doc.md").write_text(document, encoding="utf-8")
            (root / "snapshot.json").write_text(
                json.dumps({"paths": {"/{administration_id}/contacts": {"GET": "List"}}}), encoding="utf-8"
            )
            (root / "docs/checks.json").write_text(
                json.dumps(
                    {
                        "api_coverage": {
                            "snapshot": "snapshot.json",
                            "document": "doc.md",
                            "totals": {"✅": r"\*\*(\d+)\*\* dedicated"},
                        }
                    }
                ),
                encoding="utf-8",
            )
            with patch.object(CHECKER.subprocess, "check_output", return_value="doc.md\n"):
                self.assertEqual(CHECKER.document_errors(root), [])
                (root / "doc.md").write_text(
                    document.replace("/contacts", "/wrong").replace("**1**", "**2**"), encoding="utf-8"
                )
                errors = CHECKER.document_errors(root)
                self.assertTrue(any("endpoint inventory drift" in error for error in errors))
                self.assertTrue(any("coverage total drift" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
