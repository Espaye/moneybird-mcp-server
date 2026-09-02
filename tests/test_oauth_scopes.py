"""Check the OAuth scope map against Moneybird's published per-endpoint requirement.

`docs/moneybird_api_scopes.json` is generated from the official spec by
`scripts/render_api_scopes.py`; the `Required scope(s)` text it parses is exactly
what each endpoint's reference page shows. Everything here joins against that
snapshot, so a hand-written claim in `oauth_scopes.py` cannot drift from what
Moneybird actually requires — the failure mode otherwise is a 401 partway
through a real task, long after login reported success.

No network access: the snapshot is checked in.
"""
from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

from moneybird_mcp import oauth_scopes

REPO_ROOT = Path(__file__).resolve().parent.parent
SCOPES_PATH = REPO_ROOT / "docs" / "moneybird_api_scopes.json"

sys.path.insert(0, str(REPO_ROOT / "tests"))
# The generator is not importable as a package module, and several tests below
# exercise its parser directly. Inserted here rather than inside one test, so
# the import does not depend on which test happens to run first.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from test_client_spec_conformance import _client_operations  # noqa: E402

SNAPSHOT = json.loads(SCOPES_PATH.read_text(encoding="utf-8"))
OPERATIONS: dict[str, dict] = SNAPSHOT["operations"]


def _requirement(key: str) -> dict:
    return OPERATIONS[key]


def _satisfied(requirement: dict, granted: set[str]) -> bool:
    scopes = set(requirement["scopes"])
    if requirement["mode"] == "none":
        return True
    if requirement["mode"] == "any":
        return bool(scopes & granted)
    return scopes <= granted


def _client_keys() -> list[str]:
    return [f"{method} {path}" for method, path, _ in _client_operations()]


class SnapshotIntegrityTests(unittest.TestCase):
    def test_snapshot_covers_the_documented_api(self) -> None:
        self.assertGreater(len(OPERATIONS), 250)
        self.assertTrue(SNAPSHOT["info"]["version"])

    def test_every_snapshot_scope_is_a_documented_scope_name(self) -> None:
        for key, requirement in OPERATIONS.items():
            with self.subTest(operation=key):
                self.assertIn(requirement["mode"], {"all", "any", "none"})
                for scope in requirement["scopes"]:
                    self.assertIn(scope, oauth_scopes.KNOWN_SCOPES)

    def test_the_generator_still_parses_and_or_any_correctly(self) -> None:
        """'and' and 'Any of:' mean opposite things; the security array elides both."""
        from render_api_scopes import parse_requirement

        self.assertEqual(
            parse_requirement("x\n\n### Required scope(s)\n `documents` and `sales_invoices`"),
            ("all", ["documents", "sales_invoices"]),
        )
        self.assertEqual(
            parse_requirement("### Required scope(s)\n Any of: `settings`, `bank`"),
            ("any", ["settings", "bank"]),
        )
        self.assertEqual(parse_requirement("no heading here"), ("none", []))

    def test_the_scope_section_stops_at_the_next_heading(self) -> None:
        """Without a blank line the section used to run on into the next one.

        Any scope name mentioned in backticks further down — a response field, a
        cross-reference — was then read as an extra required scope, silently
        widening what the login asks for.
        """
        from render_api_scopes import parse_requirement

        self.assertEqual(
            parse_requirement(
                "### Required scope(s)\n`bank`\n"
                "### Response\nReturns `sales_invoices` objects"
            ),
            ("all", ["bank"]),
        )
        self.assertEqual(
            parse_requirement(
                "### Required scope(s)\n`documents`\n\n## Notes\nSee `bank`."
            ),
            ("all", ["documents"]),
        )

    def test_equivalent_any_of_wordings_are_all_recognised(self) -> None:
        """Reading an 'any' line as 'all' would overstate the required scopes."""
        from render_api_scopes import parse_requirement

        for prefix in ("Any of:", "Any one of:", "One of:", "ANY OF:"):
            with self.subTest(prefix=prefix):
                self.assertEqual(
                    parse_requirement(
                        f"### Required scope(s)\n{prefix} `settings`, `bank`"
                    ),
                    ("any", ["settings", "bank"]),
                )
        # A plain list still means every scope together.
        self.assertEqual(
            parse_requirement("### Required scope(s)\nAll of: `settings`, `bank`"),
            ("all", ["settings", "bank"]),
        )


class ClientEndpointCoverageTests(unittest.TestCase):
    """The requested scopes must actually cover what the client calls."""

    def test_every_client_endpoint_exists_in_the_scope_snapshot(self) -> None:
        unknown = [key for key in _client_keys() if key not in OPERATIONS]
        self.assertEqual(unknown, [])

    def test_the_default_profile_covers_every_client_endpoint(self) -> None:
        granted = set(oauth_scopes.scopes_for_profile("full"))
        unmet = [
            key
            for key in _client_keys()
            if not _satisfied(_requirement(key), granted)
        ]
        self.assertEqual(unmet, [])

    def test_every_requested_scope_is_genuinely_required(self) -> None:
        """No scope is asked for out of convenience.

        Dropping any one of the six must break at least one endpoint the client
        calls, with no substitute available. If a tool removal ever makes a scope
        unnecessary, this fails and the request should shrink.
        """
        full = set(oauth_scopes.scopes_for_profile("full"))
        for scope in oauth_scopes.KNOWN_SCOPES:
            with self.subTest(scope=scope):
                reduced = full - {scope}
                broken = [
                    key
                    for key in _client_keys()
                    if not _satisfied(_requirement(key), reduced)
                ]
                self.assertTrue(
                    broken,
                    f"{scope} is requested but no client endpoint requires it",
                )

    def test_the_scope_that_only_settings_can_satisfy_is_what_we_claim(self) -> None:
        """Pins the settings-only endpoints named in the module docstring."""
        others = set(oauth_scopes.KNOWN_SCOPES) - {"settings"}
        settings_only = {
            key
            for key in _client_keys()
            if not _satisfied(_requirement(key), others)
        }
        self.assertEqual(
            settings_only,
            {
                "GET /financial_accounts",
                "GET /products",
                "GET /products/*",
                "PATCH /products/*",
                "GET /products/identifier/*",
                "GET /projects",
                "POST /ledger_accounts",
                "PATCH /ledger_accounts/*",
                "DELETE /ledger_accounts/*",
            },
        )


class CapabilityMapAccuracyTests(unittest.TestCase):
    def test_each_area_lists_the_scopes_its_endpoints_actually_require(self) -> None:
        for entry in oauth_scopes.CAPABILITY_SCOPES:
            for endpoint in entry.endpoints:
                with self.subTest(area=entry.area, endpoint=endpoint):
                    requirement = OPERATIONS.get(endpoint)
                    self.assertIsNotNone(
                        requirement, f"{endpoint} is not a documented operation"
                    )
                    self.assertTrue(
                        _satisfied(requirement, set(entry.scopes)),
                        f"{endpoint} requires {requirement} but the area claims "
                        f"{entry.scopes}",
                    )

    def test_no_area_claims_a_scope_none_of_its_endpoints_need(self) -> None:
        """An over-claiming row is how a scope gets requested for no reason."""
        for entry in oauth_scopes.CAPABILITY_SCOPES:
            for scope in entry.scopes:
                with self.subTest(area=entry.area, scope=scope):
                    reduced = set(entry.scopes) - {scope}
                    self.assertTrue(
                        any(
                            not _satisfied(OPERATIONS[endpoint], reduced)
                            for endpoint in entry.endpoints
                        ),
                        f"{entry.area} claims {scope} but no listed endpoint needs it",
                    )

    def test_reports_are_not_described_as_needing_settings(self) -> None:
        """Moneybird assigns reports per report; none of them require settings."""
        report_keys = [key for key in OPERATIONS if key.startswith("GET /reports/")]
        self.assertTrue(report_keys)
        for key in report_keys:
            with self.subTest(report=key):
                self.assertNotIn("settings", OPERATIONS[key]["scopes"])
        for entry in oauth_scopes.CAPABILITY_SCOPES:
            if any(e.startswith("GET /reports/") for e in entry.endpoints):
                with self.subTest(area=entry.area):
                    self.assertNotIn("settings", entry.scopes)

    def test_products_and_projects_are_documented_as_settings(self) -> None:
        for endpoint in ("GET /products", "GET /projects"):
            with self.subTest(endpoint=endpoint):
                self.assertEqual(
                    OPERATIONS[endpoint], {"mode": "all", "scopes": ["settings"]}
                )

    def test_financial_accounts_are_settings_and_mutations_are_bank(self) -> None:
        """The two look alike and are scoped differently."""
        self.assertEqual(OPERATIONS["GET /financial_accounts"]["scopes"], ["settings"])
        self.assertEqual(OPERATIONS["GET /financial_mutations"]["scopes"], ["bank"])

    def test_incidental_areas_really_are_satisfiable_by_any_listed_scope(self) -> None:
        for area, scopes, endpoints in oauth_scopes.INCIDENTAL_ACCESS:
            for endpoint in endpoints:
                with self.subTest(area=area, endpoint=endpoint):
                    requirement = OPERATIONS[endpoint]
                    if not scopes:
                        self.assertEqual(requirement["mode"], "none")
                        continue
                    self.assertEqual(requirement["mode"], "any")
                    self.assertEqual(set(requirement["scopes"]), set(scopes))
                    for scope in scopes:
                        self.assertTrue(_satisfied(requirement, {scope}))

    def test_contacts_need_no_scope_of_their_own(self) -> None:
        """Any resource scope grants contacts, so contacts never widen the request."""
        for scope in ("sales_invoices", "documents", "estimates", "bank", "settings"):
            with self.subTest(scope=scope):
                self.assertTrue(_satisfied(OPERATIONS["GET /contacts"], {scope}))
        self.assertFalse(_satisfied(OPERATIONS["GET /contacts"], {"time_entries"}))

    def test_listing_administrations_needs_no_scope(self) -> None:
        """`auth login` verifies a new connection with this call."""
        self.assertTrue(_satisfied(OPERATIONS["GET /administrations"], set()))


class ProfileTests(unittest.TestCase):
    def test_narrow_profiles_lose_exactly_the_areas_advertised(self) -> None:
        self.assertEqual(
            set(oauth_scopes.unavailable_areas(
                oauth_scopes.scopes_for_profile("bookkeeping")
            )),
            {"Estimates", "Time registration"},
        )
        invoicing = set(
            oauth_scopes.unavailable_areas(oauth_scopes.scopes_for_profile("invoicing"))
        )
        self.assertIn("Purchase administration", invoicing)
        self.assertIn("Bank mutations", invoicing)
        self.assertIn("Reports: profit and loss, tax, journal entries", invoicing)
        self.assertNotIn("Sales invoicing", invoicing)
        self.assertNotIn("Reports: debtors, revenue, subscriptions", invoicing)

    def test_the_full_profile_leaves_nothing_unavailable(self) -> None:
        self.assertEqual(
            oauth_scopes.unavailable_areas(oauth_scopes.scopes_for_profile("full")), ()
        )

    def test_bookkeeping_profile_still_reaches_every_report_group(self) -> None:
        granted = set(oauth_scopes.scopes_for_profile("bookkeeping"))
        for key in (key for key in OPERATIONS if key.startswith("GET /reports/")):
            with self.subTest(report=key):
                self.assertTrue(_satisfied(OPERATIONS[key], granted))


class DocumentationAccuracyTests(unittest.TestCase):
    """The scope table in docs/oauth.md must match the code it documents."""

    def test_the_docs_scope_table_matches_the_capability_map(self) -> None:
        text = (REPO_ROOT / "docs" / "oauth.md").read_text(encoding="utf-8")
        for entry in oauth_scopes.CAPABILITY_SCOPES:
            with self.subTest(area=entry.area):
                # assertTrue, not assertIn: a failed assertIn dumps the whole
                # document into the report and buries which area is missing.
                self.assertTrue(
                    entry.area in text,
                    f"docs/oauth.md does not mention the area {entry.area!r}",
                )

    def test_the_docs_do_not_claim_reports_need_settings(self) -> None:
        text = (REPO_ROOT / "docs" / "oauth.md").read_text(encoding="utf-8")
        for line in text.splitlines():
            if re.search(r"report", line, re.I) and "`settings`" in line:
                # Only a line explicitly denying the requirement may pair them.
                self.assertRegex(line, r"[Nn]o report|not|never")


if __name__ == "__main__":
    unittest.main()
