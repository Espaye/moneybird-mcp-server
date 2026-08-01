"""Pin the licence so it cannot silently revert to plain MIT.

The project moved from MIT to MIT plus the "Commons Clause" License Condition
v1.0 and is therefore source-available, not OSI-approved open source. That
distinction lives in four places that are easy to regress independently: the
`LICENSE` text, the `pyproject.toml` metadata, the Claude Desktop bundle
manifest, and the README's user-facing statement.

A regression here is silent -- builds keep working and tests keep passing while
the project ships the wrong terms -- so each surface is asserted in both
directions: the Commons Clause condition must be present, and a bare `MIT`
declaration must not reappear.
"""
from __future__ import annotations

import json
import pathlib
import re
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# The single SPDX expression every packaging surface must agree on. The
# MIT + Commons Clause combination has no standard SPDX identifier, so PEP 639's
# custom `LicenseRef-` form is deliberate. Replacing it with a well-known id to
# satisfy a tool would misdeclare the terms.
LICENSE_EXPRESSION = "LicenseRef-MIT-Commons-Clause-1.0"

# Normative sentences of the official Commons Clause 1.0 text. Quotation marks
# are normalised before matching so retypesetting the file cannot fail the test
# for a purely cosmetic reason.
COMMONS_CLAUSE_FRAGMENTS = (
    '"Commons Clause" License Condition v1.0',
    "the grant of rights under the License will not include, and the License "
    "does not grant to you, the right to Sell the Software",
    "for a fee or other consideration",
    "whose value derives, entirely or substantially, from the functionality of "
    "the Software",
    "Any license notice or attribution required by the License must also "
    "include this Commons Clause License Condition notice",
)

MIT_FRAGMENTS = (
    "MIT License",
    "Permission is hereby granted, free of charge",
    'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND',
)


def _normalise(text: str) -> str:
    """Fold curly quotes and line wrapping so fragment matching is robust."""
    text = text.replace("“", '"').replace("”", '"')
    text = text.replace("‘", "'").replace("’", "'")
    return re.sub(r"\s+", " ", text)


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


class LicenseFileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.raw = _read("LICENSE")
        self.text = _normalise(self.raw)

    def test_commons_clause_condition_is_present_in_full(self) -> None:
        for fragment in COMMONS_CLAUSE_FRAGMENTS:
            with self.subTest(fragment=fragment[:40]):
                self.assertIn(_normalise(fragment), self.text)

    def test_underlying_mit_grant_is_retained(self) -> None:
        for fragment in MIT_FRAGMENTS:
            with self.subTest(fragment=fragment[:40]):
                self.assertIn(_normalise(fragment), self.text)

    def test_clause_parameters_name_this_project_and_licensor(self) -> None:
        self.assertIn("Software: moneybird-mcp-server", self.text)
        self.assertIn("License: MIT", self.text)
        self.assertIn("Licensor: Espaye", self.text)

    def test_license_is_not_unconditional_mit(self) -> None:
        """A bare MIT file would start with the MIT header and never mention the
        condition. Guard the exact regression this test exists for."""
        self.assertFalse(self.raw.lstrip().startswith("MIT License"))
        self.assertIn("Commons Clause", self.raw)


class PackageMetadataLicenseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.pyproject = tomllib.loads(_read("pyproject.toml"))
        self.project = self.pyproject["project"]

    def test_declares_the_custom_license_expression(self) -> None:
        self.assertEqual(self.project["license"], LICENSE_EXPRESSION)

    def test_does_not_declare_plain_mit(self) -> None:
        self.assertNotEqual(self.project["license"], "MIT")

    def test_license_file_ships_in_both_distributions(self) -> None:
        """`license-files` is what puts LICENSE in the sdist root and in the
        wheel's `.dist-info/licenses/`; the sdist `include` list does not need
        to repeat it. Verified against built artifacts by the CI package job."""
        declared = self.project["license-files"]
        self.assertIn("LICENSE", declared)
        for entry in declared:
            with self.subTest(entry=entry):
                self.assertTrue(
                    (ROOT / entry).is_file(),
                    f"license-files references {entry!r}, which does not exist",
                )

    def test_no_license_trove_classifier_conflicts_with_the_expression(self) -> None:
        """PEP 639 forbids combining a license expression with `License ::`
        classifiers, and hatchling errors out on the combination."""
        classifiers = self.project.get("classifiers", [])
        self.assertEqual([c for c in classifiers if c.startswith("License ::")], [])

    def test_expression_is_a_valid_spdx_expression(self) -> None:
        try:
            from packaging.licenses import canonicalize_license_expression
        except ImportError:  # pragma: no cover - older packaging without PEP 639
            self.skipTest("packaging is too old to validate license expressions")
        self.assertEqual(
            canonicalize_license_expression(self.project["license"]),
            LICENSE_EXPRESSION,
        )


class BundleManifestLicenseTests(unittest.TestCase):
    def test_manifest_matches_the_package_license(self) -> None:
        manifest = json.loads(_read("mcpb/manifest.json"))
        pyproject = tomllib.loads(_read("pyproject.toml"))
        self.assertEqual(manifest["license"], LICENSE_EXPRESSION)
        self.assertEqual(manifest["license"], pyproject["project"]["license"])

    def test_manifest_does_not_declare_plain_mit(self) -> None:
        manifest = json.loads(_read("mcpb/manifest.json"))
        self.assertNotEqual(manifest["license"], "MIT")


class ReadmeLicensingStatementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.readme = _normalise(_read("README.md"))

    def test_states_the_licence_and_that_it_is_source_available(self) -> None:
        self.assertIn("Commons Clause", self.readme)
        self.assertIn("source-available", self.readme)

    def test_does_not_claim_osi_open_source(self) -> None:
        """The only permitted use of "open source" is the explicit denial."""
        for match in re.finditer(r"open.{0,2}source", self.readme, re.IGNORECASE):
            window = self.readme[max(0, match.start() - 40) : match.end() + 10]
            with self.subTest(context=window):
                self.assertRegex(window, r"(?i)not\b[^.]*open.{0,2}source")

    def test_points_commercial_enquiries_at_the_repository_owner(self) -> None:
        self.assertIn("commercial licensing", self.readme.lower())
        self.assertIn("Espaye/moneybird-mcp-server", self.readme)


class DutchReadmeLicensingStatementTests(unittest.TestCase):
    """The translated README is a second user-facing licence statement.

    It ships in the sdist, so a translation that softened the terms would be
    distributed as fact. The English assertions cannot be reused: Dutch denies
    the OSI claim with "geen"/"niet", which the English negation regex misses.
    """

    def setUp(self) -> None:
        self.readme = _normalise(_read("README.nl.md"))

    def test_states_the_licence_and_that_it_is_source_available(self) -> None:
        self.assertIn("Commons Clause", self.readme)
        self.assertIn("source-available", self.readme)

    def test_does_not_claim_osi_open_source(self) -> None:
        """The only permitted use of "open source" is the explicit denial."""
        for match in re.finditer(r"open.{0,2}source", self.readme, re.IGNORECASE):
            window = self.readme[max(0, match.start() - 40) : match.end() + 10]
            with self.subTest(context=window):
                self.assertRegex(window, r"(?i)\b(geen|niet)\b[^.]*open.{0,2}source")

    def test_points_commercial_enquiries_at_the_repository_owner(self) -> None:
        self.assertIn("commerciële licenties", self.readme.lower())
        self.assertIn("Espaye/moneybird-mcp-server", self.readme)


if __name__ == "__main__":
    unittest.main()
