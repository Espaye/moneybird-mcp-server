"""The always-loaded server instructions must fit where clients actually read them.

Claude Code cuts MCP server instructions off at about 2,048 characters. The
instructions were 8,932, so the model there never saw the known API limits and
most of the working method. They now hold the identity line, the five hard
rules and pointers; every other fact moved to the tool description or
bookkeeping-guide topic where it is needed. These tests pin the budget, and
pin each moved fact to its new home so a later edit cannot silently drop it.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ.setdefault(
    "MONEYBIRD_MCP_DATA_DIR",
    tempfile.mkdtemp(prefix="moneybird_mcp_test_state_"),
)
os.environ.setdefault("MONEYBIRD_ACCESS_TOKEN", "x")
os.environ.setdefault("MONEYBIRD_ADMINISTRATION_ID", "1")

import moneybird_mcp.tools  # noqa: E402,F401  (registers the full catalogue)
from moneybird_mcp.guidance import playbook_topic  # noqa: E402
from moneybird_mcp.tools._registry import SERVER_INSTRUCTIONS, mcp  # noqa: E402

#: The whole always-loaded text.
INSTRUCTIONS_BUDGET = 1_500
#: Where Claude Code's cut falls, less a margin for its truncation marker.
CLIENT_CUTOFF = 2_000


def _hard_rules_end(text: str) -> int:
    """Index just past hard rule 5, the last one."""
    match = re.search(r"^5\. .*$", text, flags=re.MULTILINE)
    assert match is not None, "hard rule 5 is missing"
    return match.end()


class InstructionBudgetTests(unittest.TestCase):
    def test_instructions_fit_the_budget(self) -> None:
        self.assertLessEqual(len(SERVER_INSTRUCTIONS), INSTRUCTIONS_BUDGET)

    def test_all_five_hard_rules_are_present_in_order(self) -> None:
        positions = [
            SERVER_INSTRUCTIONS.find(f"\n{number}. ") for number in range(1, 6)
        ]
        self.assertNotIn(-1, positions)
        self.assertEqual(positions, sorted(positions))
        self.assertLess(positions[0], SERVER_INSTRUCTIONS.index("HARD RULES") + 40)

    def test_hard_rules_keep_their_full_substance(self) -> None:
        rules = SERVER_INSTRUCTIONS[: _hard_rules_end(SERVER_INSTRUCTIONS)]
        flat = " ".join(rules.split())
        for phrase in (
            # 1: the approval flow, the compact-discovery proxy, and why
            # request-context writes stay off.
            "Explicit user confirmation is mandatory",
            "prepare_* tool -> show the preview -> wait for a clear \"yes\"",
            "execute_approved_action with the returned approval_id",
            "call_tool is read-only",
            "call execute_approved_action directly",
            "destructive annotation",
            "model-callable, not proof of human intent",
            "request-context writes stay disabled",
            # 2
            "Never invent data (invoice numbers, references, amounts, dates, "
            "counterparties)",
            "ask or leave it blank",
            # 3
            "report the returned verification evidence and any gap",
            "to the cent",
            # 4
            "propose with reasoning and ask for approval; never guess silently",
            # 5
            "not an accountant or tax advisor",
            "defer fiscal judgment calls to the bookkeeper",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase.lower(), flat.lower())

    def test_hard_rules_end_well_inside_the_budget(self) -> None:
        self.assertLessEqual(_hard_rules_end(SERVER_INSTRUCTIONS), 1_000)

    def test_hard_rules_survive_the_setup_banner(self) -> None:
        # An unconfigured server prepends SETUP INCOMPLETE to these same
        # instructions. That banner shares the client's budget, so the rules
        # must still end before the cut. A named OAuth profile makes the
        # banner longest, so measure with one set.
        from moneybird_mcp import server as server_module

        class _Server:
            instructions = SERVER_INSTRUCTIONS

        for mode in ("local", "network_single_user"):
            with self.subTest(mode=mode):
                server = _Server()
                with mock.patch.dict(
                    os.environ, {"MONEYBIRD_OAUTH_PROFILE": "administratie"}
                ), mock.patch(
                    "moneybird_mcp.credentials.credentials_are_configured",
                    return_value=False,
                ):
                    server_module._announce_missing_credentials(mode, server)
                self.assertTrue(server.instructions.startswith("\nSETUP INCOMPLETE"))
                self.assertLessEqual(
                    _hard_rules_end(server.instructions), CLIENT_CUTOFF
                )

    def test_pointers_name_where_the_rest_lives(self) -> None:
        for pointer in (
            "get_bookkeeping_guide(topic)",
            "moneybird://playbook/bookkeeping",
            "suggest_bank_mutation_matches",
            "get_server_status",
            "aan_de_slag",
            'get_bookkeeping_guide("grenzen")',
        ):
            with self.subTest(pointer=pointer):
                self.assertIn(pointer, SERVER_INSTRUCTIONS)


def _tool_texts() -> dict[str, str]:
    """Each tool's description plus its input schema, as the model sees them."""
    tools = asyncio.run(mcp.list_tools())
    return {
        tool.name: " ".join(
            f"{tool.description or ''} {json.dumps(tool.parameters)}".split()
        )
        for tool in tools
    }


def _compact_call_tool_text() -> str:
    from fastmcp import FastMCP

    from moneybird_mcp.tool_discovery import configure_tool_discovery

    server = FastMCP("instructions-test")
    configure_tool_discovery(server, "search")
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    return " ".join((tools["call_tool"].description or "").split())


def _guide(topic: str) -> str:
    result = playbook_topic(topic)
    assert "error" not in result, result
    return " ".join(str(result["guidance"]).split())


class MovedFactsTests(unittest.TestCase):
    """Every fact that left the instructions is findable at its new home.

    Each row: (what the fact is, where it lives, phrases that must be there).
    Phrases are matched case-insensitively with whitespace collapsed.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.tools = _tool_texts()

    def _home(self, kind: str, name: str) -> str:
        if kind == "tool":
            self.assertIn(name, self.tools, f"tool {name} is not registered")
            return self.tools[name]
        if kind == "guide":
            return _guide(name)
        if kind == "compact":
            return _compact_call_tool_text()
        raise AssertionError(kind)

    MOVED = [
        # --- from HOW TO WORK ---
        ("guide holds domain rules the API does not express",
         "tool", "get_bookkeeping_guide",
         ["does not express", "grenzen", "btw_afwikkeling", "bankmutaties"]),
        ("the guide topics can be listed",
         "tool", "list_bookkeeping_guide_topics", ["topics"]),
        ("bank matching is deterministic on four signals",
         "tool", "suggest_bank_mutation_matches",
         ["invoice reference", "exact open amount", "counterparty IBAN",
          "contact name", "evidence"]),
        ("group matches cover one purchase invoice's complete open balance",
         "tool", "suggest_bank_mutation_matches",
         ["group_matches", "complete open balance",
          "prepare_settle_purchase_invoice_from_bank_mutations"]),
        ("do not redo the matching by hand",
         "tool", "suggest_bank_mutation_matches",
         ["Do not redo this matching by hand from reports and invoice lists"]),
        ("ambiguous or none: ask; none usually means a ledger account",
         "tool", "suggest_bank_mutation_matches",
         ["'ambiguous' or 'none', ask the user",
          "'none' usually means the amount belongs on a ledger account"]),
        ("a strong group match settles in one approval, keeps lines, verifies paid",
         "tool", "prepare_settle_purchase_invoice_from_bank_mutations",
         ["strong group_match", "One approval links the complete group",
          "still-new invoice without changing its accounting lines",
          "final paid state"]),
        ("compact discovery: search_tools, call_tool, always-visible core",
         "compact", "call_tool",
         ["search_tools", "read or prepare tool", "search", "fetch",
          "sync_search_index", "get_server_status",
          "prepare_bookkeeping_correction_batch", "execute_approved_action",
          "stay directly visible", "default mode is the full catalogue"]),
        ("search hits carry enough that fetch is rarely needed",
         "tool", "search",
         ["date, amount, state, and contact_id", "fetch is usually unnecessary"]),
        ("fetch is for the full record of a known id",
         "tool", "fetch", ["full record"]),
        ("an exact purchase-invoice reference uses the direct lookup",
         "tool", "get_purchase_invoice_by_reference",
         ["exact supplier reference", "instead of broad search", "attachment",
          "payment", "version"]),
        ("get_financial_report covers every report",
         "tool", "get_financial_report",
         ["profit_loss", "balance_sheet", "general_ledger", "cash_flow", "tax",
          "debtors", "creditors", "aging", "revenue_by_contact",
          "expenses_by_project", "journal_entries", "subscriptions", "assets"]),
        ("read-only endpoints without a tool go through moneybird_request",
         "tool", "moneybird_request",
         ["without a dedicated tool", "subscriptions", "identities",
          "document styles", "workflows", "users", "custom fields",
          "can ONLY read"]),
        ("rate limits: 150 and 50 per 5 minutes, per IP",
         "guide", "grenzen",
         ["per IP-adres", "150 verzoeken per 5 minuten", "/reports/",
          "50 per 5 minuten"]),
        ("prefer broad reads and the sync index over rescans",
         "guide", "grenzen",
         ["één brede leesactie dan veel smalle", "sync-index in plaats van opnieuw"]),
        ("status reports the remaining budget; a refusal names the bucket",
         "tool", "get_server_status",
         ["calls start failing", "150 requests per 5 minutes", "50 for /reports/",
          "observed remaining budget", "names the bucket and when it frees up"]),
        ("reports have their own tighter bucket",
         "tool", "get_financial_report", ["50 requests per 5 minutes"]),
        ("mixed corrections: one preview, global preflight, no transaction",
         "tool", "prepare_bookkeeping_correction_batch",
         ["one exact preview", "preflights every child before the first write",
          "no cross-object transaction", "partial result"]),
        # --- from KNOWN LIMITS ---
        ("booking rules are not in the API; infer from fields and timing",
         "guide", "grenzen",
         ["Boekingsregels (bankregels) zitten niet in de API",
          "transaction_rules", "bank_rules", "404", "state", "payments",
          "ledger_account_bookings", "created_at", "processed_at",
          "Zeg ronduit wat je niet kunt zien", "Instellingen → Boekhouding → Boekingsregels",
          "recept E", "diagnose_bankmutatie"]),
        ("booking rules botch purchase invoices inconsistently",
         "guide", "grenzen",
         ["inkoopfacturen, en inconsistent", "één verzamelregel", "status `new`",
          "prices_are_incl_tax", "review_purchase_invoices",
          "prepare_reconcile_purchase_invoice", "desired_lines",
          "read_document_attachment"]),
        ("review finds the booking-rule damage",
         "tool", "review_purchase_invoices",
         ["booking rules", "inconsistently", "single catch-all line",
          "prices_are_incl_tax", "prepare_reconcile_purchase_invoice"]),
        ("reconcile: scaled to the cent, flagged assumption, PDF lines, version",
         "tool", "prepare_reconcile_purchase_invoice",
         ["keeping the document total to the cent", "flagged assumption",
          "read_document_attachment", "desired_lines",
          "refuses any allocation that changes the current total",
          "execution aborts if the invoice changes after the preview"]),
        ("a wide mutation period is rejected; query per month",
         "tool", "list_financial_mutations",
         ["HTTP 400", "per month", "YYYYMM01..YYYYMMnn", "sync index"]),
        ("a wide mutation period is rejected (guide)",
         "guide", "grenzen",
         ["HTTP 400", "JJJJMM01..JJJJMMnn", "sync-index"]),
        ("complete_scan proves the population; a provider page does not",
         "tool", "list_financial_mutations",
         ["complete_scan=true", "synchronization", "exact-ID", "state locally",
          "hide non-settled rows"]),
        ("complete_scan (guide)",
         "guide", "grenzen",
         ["providerpagina", "complete_scan=true", "expliciete periode",
          "niet-afgewikkelde mutaties", "review_purchase_invoices"]),
        ("review has the same complete_scan flag",
         "tool", "review_purchase_invoices",
         ["complete_scan=true", "'all clear'", "one provider page"]),
        ("report period limits",
         "tool", "get_financial_report",
         ["cash_flow, tax, debtors, and creditors accept at most one month",
          "aging reports take a whole month",
          "Only profit_loss, balance_sheet, general_ledger, and the "
          "by_contact/by_project reports accept a wide period like this_year"]),
        ("report period limits (guide)",
         "guide", "grenzen",
         ["maximaal **één maand**", "*_aging", "profit_loss", "balance_sheet",
          "general_ledger", "by_contact", "this_year"]),
        # --- from SYNC INDEX ---
        ("search falls back to a partial live scan; sync first",
         "tool", "search",
         ["live scan that is partial", "breaks on large data",
          "backlog, categorize, or whole-year task", "live_fallback", "warnings",
          "run sync_search_index once, then search again"]),
        ("the index is a per-administration snapshot; refresh is cheap",
         "tool", "sync_search_index",
         ["per administration", "point-in-time snapshot",
          "after making changes", "recent data", "only fetches changed records"]),
        ("sync index (guide)",
         "guide", "sync_index",
         ["live_fallback", "warnings", "sync_search_index"]),
    ]

    def test_every_moved_fact_is_at_its_new_home(self) -> None:
        for fact, kind, name, phrases in self.MOVED:
            home = self._home(kind, name).lower()
            for phrase in phrases:
                with self.subTest(fact=fact, home=f"{kind}:{name}", phrase=phrase):
                    self.assertIn(" ".join(phrase.split()).lower(), home)

    def test_the_guarded_write_pairs_are_all_served(self) -> None:
        # The old instructions listed these; the tool list itself now does.
        for name in (
            "prepare_register_payment",
            "prepare_link_bank_mutation_booking",
            "prepare_unlink_bank_mutation_booking",
            "prepare_reclassify_bank_mutation_bookings",
            "prepare_settle_purchase_invoice_from_bank_mutations",
            "prepare_create_credit_invoice",
            "prepare_bookkeeping_correction_batch",
            "execute_approved_action",
        ):
            with self.subTest(tool=name):
                self.assertIn(name, self.tools)

    def test_the_named_scenario_prompts_are_all_served(self) -> None:
        prompts = {prompt.name for prompt in asyncio.run(mcp.list_prompts())}
        for name in (
            "aan_de_slag",
            "verwerk_achterstand",
            "categoriseer_heel_jaar",
            "leg_cijfers_uit",
            "diagnose_bankmutatie",
            "koppel_banktransacties",
            "factureer_meterverbruik",
        ):
            with self.subTest(prompt=name):
                self.assertIn(name, prompts)

    def test_the_playbook_resource_is_served(self) -> None:
        resources = {str(item.uri) for item in asyncio.run(mcp.list_resources())}
        self.assertIn("moneybird://playbook/bookkeeping", resources)


if __name__ == "__main__":
    unittest.main()
