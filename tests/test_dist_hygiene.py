"""The packaging guard CI relies on: scripts/check_dist_hygiene.py.

A check that can never fail is worthless, so these tests pin both directions:
clean artifacts pass, and every class the gate claims to catch actually fails --
stray paths, packaged secrets, record identifiers, bank account numbers,
deliverable e-mail addresses, populated registration fields, and absolute
developer paths.

Every canary is *composed at runtime* rather than written as a literal. That is
not ceremony: this file ships inside the sdist the gate inspects, so a stored
canary would make the real artifact fail its own check. Having to build one is
the gate proving it is strict enough to catch a leak in its own test suite, and
it keeps the fixtures free of anything resembling real data.
"""
from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import shutil
import tarfile
import tempfile
import tomllib
import unittest
import zipfile
from unittest import mock

_SCRIPT = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "check_dist_hygiene.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("check_dist_hygiene", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_dist_hygiene = _load_script()

VERSION = "9.9.9"
CLEAN_WHEEL = ["moneybird_mcp/__init__.py", f"moneybird_mcp-{VERSION}.dist-info/METADATA"]
CLEAN_SDIST = [
    f"moneybird_mcp-{VERSION}/pyproject.toml",
    f"moneybird_mcp-{VERSION}/moneybird_mcp/__init__.py",
    f"moneybird_mcp-{VERSION}/tests/test_x.py",
]


# --- canaries, all built rather than stored -------------------------------


def unplaceheld_identifier() -> bytes:
    """An 18-digit id that no placeholder shape admits."""
    return str(4 * 10**17 + 42).encode()


def placeholder_identifiers() -> list[bytes]:
    """One of each documented placeholder shape."""
    return [b"100000000000000042", b"777777777777777777", b"123456789012345678"]


def checksum_valid_iban(country: str = "NL", bban: str = "BANK0123456789") -> bytes:
    """An IBAN for a bank that does not exist, with real ISO 13616 check digits."""
    rearranged = f"{bban}{country}00"
    numeric = "".join(
        str(ord(character) - ord("A") + 10) if character.isalpha() else character
        for character in rearranged
    )
    return f"{country}{98 - int(numeric) % 97:02d}{bban}".encode()


def deliverable_address(local: str = "canary", domain: str = "hygiene-canary.nl") -> bytes:
    """An address shaped like one a message could reach."""
    return f"{local}@{domain}".encode()


def labelled_registration(field: str = "chamber_of_commerce", value: str = "12345678") -> bytes:
    """A populated registration field, in the JSON shape Moneybird returns."""
    return f'{{"{field}": "{value}"}}'.encode()


def absolute_windows_path(*parts: str) -> bytes:
    """A developer path that names a machine and an account."""
    return "\\".join(parts or ("C:", "Users", "canary", "state.json")).encode()


def _build_dist(
    tmp: pathlib.Path,
    wheel: list[str],
    sdist: list[str],
    contents: dict[str, bytes] | None = None,
) -> pathlib.Path:
    """Write a synthetic wheel + sdist holding the given entry names."""
    contents = contents or {}
    with zipfile.ZipFile(tmp / f"moneybird_mcp-{VERSION}-py3-none-any.whl", "w") as zf:
        for name in wheel:
            zf.writestr(name, contents.get(name, b""))
    with tarfile.open(tmp / f"moneybird_mcp-{VERSION}.tar.gz", "w:gz") as tf:
        for name in sdist:
            payload = contents.get(name, b"")
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))
    return tmp


class DistHygieneTestCase(unittest.TestCase):
    def _check(
        self,
        wheel: list[str],
        sdist: list[str],
        contents: dict[str, bytes] | None = None,
    ) -> list[str]:
        with tempfile.TemporaryDirectory() as raw:
            dist = _build_dist(pathlib.Path(raw), wheel, sdist, contents)
            return check_dist_hygiene.check(dist)

    def _check_sdist_payload(self, payload: bytes) -> list[str]:
        """Run the gate over one allowed sdist file holding the given bytes."""
        entry = f"moneybird_mcp-{VERSION}/tests/test_x.py"
        return self._check(CLEAN_WHEEL, CLEAN_SDIST, {entry: payload})


class PathPolicyTests(DistHygieneTestCase):
    def test_clean_artifacts_pass(self) -> None:
        self.assertEqual(self._check(CLEAN_WHEEL, CLEAN_SDIST), [])

    def test_sdist_may_ship_env_example(self) -> None:
        # .env.example holds no values and is included on purpose.
        problems = self._check(
            CLEAN_WHEEL, [*CLEAN_SDIST, f"moneybird_mcp-{VERSION}/.env.example"]
        )
        self.assertEqual(problems, [])

    def test_wheel_rejects_entries_outside_the_package(self) -> None:
        # The sdist ships tests/ and docs/ on purpose; the wheel must not.
        problems = self._check([*CLEAN_WHEEL, "tests/test_x.py"], CLEAN_SDIST)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("unexpected wheel path", problems[0])
        self.assertIn("tests/test_x.py", problems[0])

    def test_sdist_rejects_a_path_outside_the_published_set(self) -> None:
        problems = self._check(
            CLEAN_WHEEL, [*CLEAN_SDIST, f"moneybird_mcp-{VERSION}/notes/private.md"]
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("unexpected sdist path", problems[0])

    def test_packaged_secrets_are_named_not_merely_rejected(self) -> None:
        for leaked in (
            "moneybird_mcp/.env",
            "moneybird_mcp/moneybird_oauth_tokens.json",
            "moneybird_mcp/moneybird_approvals.sqlite3",
            "moneybird_mcp/.moneybird_sync_index_123.json",
            "moneybird_mcp/.moneybird_search_fts_123.sqlite3",
            "moneybird_mcp/.moneybird_audit_log_123.jsonl",
        ):
            with self.subTest(leaked=leaked):
                problems = self._check([*CLEAN_WHEEL, leaked], CLEAN_SDIST)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("sensitive file", problems[0])
                self.assertIn(leaked, problems[0])

    def test_sdist_secrets_are_flagged(self) -> None:
        problems = self._check(
            CLEAN_WHEEL,
            [*CLEAN_SDIST, f"moneybird_mcp-{VERSION}/.moneybird_audit_log.jsonl"],
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("sensitive file", problems[0])

    def test_an_identifier_in_a_packaged_filename_is_caught(self) -> None:
        """A dump named after a record leaks through the listing alone."""
        name = f"moneybird_mcp-{VERSION}/tests/{unplaceheld_identifier().decode()}.py"
        problems = self._check(CLEAN_WHEEL, [*CLEAN_SDIST, name])
        self.assertTrue(
            any("the path" in problem and "identifier" in problem for problem in problems),
            problems,
        )

    def test_missing_or_ambiguous_artifacts_are_reported(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            empty = pathlib.Path(raw)
            problems = check_dist_hygiene.check(empty)
            self.assertEqual(len(problems), 1)
            self.assertIn("expected exactly one wheel and one sdist", problems[0])

            # A stale previous version left behind makes the release ambiguous.
            _build_dist(empty, CLEAN_WHEEL, CLEAN_SDIST)
            (empty / "moneybird_mcp-0.0.1-py3-none-any.whl").write_bytes(b"")
            problems = check_dist_hygiene.check(empty)
            self.assertEqual(len(problems), 1)
            self.assertIn("expected exactly one wheel and one sdist", problems[0])


class StructuralContentTests(DistHygieneTestCase):
    def test_identifier_outside_the_placeholder_shapes_is_flagged(self) -> None:
        problems = self._check_sdist_payload(unplaceheld_identifier())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("synthetic placeholder shapes", problems[0])

    def test_documented_placeholder_identifiers_pass(self) -> None:
        for placeholder in placeholder_identifiers():
            with self.subTest(placeholder=placeholder):
                self.assertEqual(self._check_sdist_payload(placeholder), [])

    def test_a_longer_number_is_not_sliced_into_an_identifier(self) -> None:
        # Twenty digits is not an id with two spare digits on the end.
        self.assertEqual(self._check_sdist_payload(b"12345678901234567890"), [])

    def test_checksum_valid_bank_account_number_is_flagged(self) -> None:
        problems = self._check_sdist_payload(checksum_valid_iban())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("bank account number", problems[0])

    def test_structurally_invalid_account_number_is_ignored(self) -> None:
        # Same shape, wrong check digits: a placeholder, not an account.
        self.assertEqual(self._check_sdist_payload(b"NL00BANK0123456789"), [])

    def test_an_uppercase_word_cannot_reach_the_checksum_test(self) -> None:
        self.assertEqual(self._check_sdist_payload(b"GB12ABCDEFGHIJKLMNOP"), [])

    def test_deliverable_email_address_is_flagged(self) -> None:
        problems = self._check_sdist_payload(deliverable_address())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("e-mail address", problems[0])

    def test_reserved_documentation_domains_pass(self) -> None:
        for domain in ("example.com", "example.org", "storage.example", "host.invalid"):
            with self.subTest(domain=domain):
                payload = deliverable_address(domain=domain)
                self.assertEqual(self._check_sdist_payload(payload), [])

    def test_a_placeholder_without_a_real_top_level_domain_is_not_an_address(self) -> None:
        self.assertEqual(self._check_sdist_payload(deliverable_address(domain="b.c")), [])

    def test_populated_registration_field_is_flagged(self) -> None:
        for field in ("chamber_of_commerce", "tax_number"):
            with self.subTest(field=field):
                problems = self._check_sdist_payload(labelled_registration(field))
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("registration field", problems[0])
                self.assertIn(field, problems[0])

    def test_empty_registration_field_is_ignored(self) -> None:
        payload = labelled_registration(value="")
        self.assertEqual(self._check_sdist_payload(payload), [])

    def test_absolute_developer_path_is_flagged(self) -> None:
        problems = self._check_sdist_payload(absolute_windows_path())
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("absolute Windows path", problems[0])

    def test_documented_operator_placeholder_paths_pass(self) -> None:
        payload = absolute_windows_path("C:", "absolute", "operator.env")
        self.assertEqual(self._check_sdist_payload(payload), [])

    def test_the_gate_carries_no_private_value_of_its_own(self) -> None:
        """The script must recognise shapes, never remembered examples.

        Running the content rules over the gate's own source is the cheapest way
        to keep it honest: the moment somebody pastes a real identifier, account
        number or address in to 'make sure we catch this one', its own test fails.
        """
        problems = check_dist_hygiene._identifier_problems(
            "check_dist_hygiene.py", _SCRIPT.read_bytes()
        )
        self.assertEqual(problems, [])


class MarkerFileTests(DistHygieneTestCase):
    def _with_markers(self, path: str):
        return mock.patch.dict(
            os.environ, {check_dist_hygiene.MARKERS_FILE_ENV: path}
        )

    def test_the_gate_works_without_a_marker_file(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(check_dist_hygiene.MARKERS_FILE_ENV, None)
            self.assertEqual(self._check(CLEAN_WHEEL, CLEAN_SDIST), [])

    def test_a_configured_marker_is_matched_case_insensitively(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            markers = pathlib.Path(raw) / "markers.json"
            markers.write_text(json.dumps({"literals": ["Zeldzame Naam"]}), encoding="utf-8")
            with self._with_markers(str(markers)):
                problems = self._check_sdist_payload(b"... zeldzame naam ...")
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("configured private marker", problems[0])

    def test_a_missing_marker_file_fails_the_gate(self) -> None:
        """A typo in the path must not silently switch the extra layer off."""
        with self._with_markers(str(pathlib.Path(tempfile.gettempdir()) / "absent.json")):
            problems = self._check(CLEAN_WHEEL, CLEAN_SDIST)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn(check_dist_hygiene.MARKERS_FILE_ENV, problems[0])

    def test_a_malformed_marker_file_fails_the_gate(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            markers = pathlib.Path(raw) / "markers.json"
            markers.write_text("{not json", encoding="utf-8")
            with self._with_markers(str(markers)):
                problems = self._check(CLEAN_WHEEL, CLEAN_SDIST)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("not valid JSON", problems[0])

    def test_marker_literals_must_be_strings(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            markers = pathlib.Path(raw) / "markers.json"
            markers.write_text(json.dumps({"literals": [17]}), encoding="utf-8")
            with self._with_markers(str(markers)):
                problems = self._check(CLEAN_WHEEL, CLEAN_SDIST)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("must be a list of strings", problems[0])


class RealArtifactTests(DistHygieneTestCase):
    def test_real_artifacts_pass_when_present(self) -> None:
        """If dist/ holds a real build, it must be clean too."""
        dist = _SCRIPT.parent.parent / "dist"
        project = tomllib.loads(
            (_SCRIPT.parent.parent / "pyproject.toml").read_text(encoding="utf-8")
        )
        version = project["project"]["version"]
        current_wheels = list(dist.glob(f"moneybird_mcp-{version}-*.whl"))
        current_sdists = list(dist.glob(f"moneybird_mcp-{version}.tar.gz"))
        if len(current_wheels) != 1 or len(current_sdists) != 1:
            self.skipTest(f"no single freshly built {version} wheel/sdist in dist/")
        with tempfile.TemporaryDirectory() as raw:
            current = pathlib.Path(raw)
            shutil.copy2(current_wheels[0], current / current_wheels[0].name)
            shutil.copy2(current_sdists[0], current / current_sdists[0].name)
            self.assertEqual(check_dist_hygiene.check(current), [])


if __name__ == "__main__":
    unittest.main()
