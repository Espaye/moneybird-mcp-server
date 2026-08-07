"""The playbook must stay reachable as a tool, per topic, with its gotchas intact.

An MCP *resource* is read by the client, not the model: Claude Desktop needs the
user to attach it and ChatGPT connectors do not read arbitrary resources. These
tests pin the tool path, which is the one that works everywhere.
"""
from __future__ import annotations

import os
import tempfile
import unittest

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)

from moneybird_mcp.guidance import (
    PLAYBOOK_TOPICS,
    _playbook_sections,
    load_playbook,
    playbook_topic,
)


class PlaybookTopicTests(unittest.TestCase):
    def test_every_topic_resolves_to_real_content(self):
        # The playbook is hand-edited; a renamed heading must fail here rather
        # than silently return an empty guide to a user mid-task.
        for topic in PLAYBOOK_TOPICS:
            with self.subTest(topic=topic):
                result = playbook_topic(topic)
                self.assertNotIn("error", result, topic)
                self.assertGreater(len(result["guidance"]), 200, topic)

    def test_unknown_topic_lists_the_valid_ones(self):
        result = playbook_topic("belastingaangifte")
        self.assertIn("error", result)
        self.assertEqual(set(result["topics"]), set(PLAYBOOK_TOPICS))

    def test_empty_topic_is_a_listing_not_a_crash(self):
        self.assertIn("topics", playbook_topic(""))

    def test_topic_is_normalised(self):
        self.assertEqual(playbook_topic("BTW-Afwikkeling")["topic"], "btw_afwikkeling")

    def test_btw_topic_keeps_the_rounding_rule(self):
        # 'Afronding' is an unnumbered ### under '## 3. BTW'. If the section
        # parser ever splits it out, the single most misread rule in the document
        # disappears from the topic that exists to carry it.
        guidance = playbook_topic("btw")["guidance"]
        self.assertIn("hele euro", guidance)
        self.assertIn("Hardcodeer geen tolerantie", guidance)

    def test_bank_topic_routes_to_the_matcher(self):
        self.assertIn(
            "suggest_bank_mutation_matches", playbook_topic("bankmutaties")["guidance"]
        )

    def test_vat_settlement_topic_carries_the_reverse_charge_warning(self):
        self.assertIn("verlegde", playbook_topic("btw_afwikkeling")["guidance"])

    def test_sections_cover_the_whole_playbook(self):
        # Every '## ' heading must be addressable, so no part of the document can
        # become unreachable without a test failing.
        headings = [
            line for line in load_playbook().splitlines() if line.startswith("## ")
        ]
        self.assertEqual(len(_playbook_sections()) - _numbered_subsections(), len(headings))

    def test_topics_do_not_overlap(self):
        seen: set[str] = set()
        for keys, _ in PLAYBOOK_TOPICS.values():
            for key in keys:
                self.assertNotIn(key, seen, f"section {key} is in two topics")
                seen.add(key)


def _numbered_subsections() -> int:
    return sum(
        1
        for line in load_playbook().splitlines()
        if line.startswith("### ") and line[4:5].isalnum() and line[5:6] in {".", " "}
    )


class GuideToolRegistrationTests(unittest.TestCase):
    def test_tool_is_registered_and_read_only(self):
        os.environ.setdefault("MONEYBIRD_ACCESS_TOKEN", "x")
        os.environ.setdefault("MONEYBIRD_ADMINISTRATION_ID", "1")
        import asyncio

        import moneybird_mcp.tools  # noqa: F401  (registers the catalogue)
        from moneybird_mcp.tools._registry import mcp

        tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
        self.assertIn("get_bookkeeping_guide", tools)
        self.assertIn("list_bookkeeping_guide_topics", tools)
        self.assertIs(tools["get_bookkeeping_guide"].annotations.readOnlyHint, True)

    def test_tool_description_names_dutch_phrasings(self):
        from moneybird_mcp.tools.catalogue import get_bookkeeping_guide

        description = (get_bookkeeping_guide.__doc__ or "").lower()
        for term in ("btw", "bankmutatie", "grootboek", "factur"):
            self.assertIn(term, description)


if __name__ == "__main__":
    unittest.main()
