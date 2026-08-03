"""The packaging guard CI relies on: scripts/check_dist_hygiene.py.

A check that can never fail is worthless, so these tests pin both directions --
clean artifacts pass, and each class of leak (stray top-level entry, `.env`,
tokens, approvals DB, sync cache, audit log) is caught.
"""
from __future__ import annotations

import importlib.util
import io
import pathlib
import shutil
import tarfile
import tempfile
import tomllib
import unittest
import zipfile

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


def _build_dist(tmp: pathlib.Path, wheel: list[str], sdist: list[str]) -> pathlib.Path:
    """Write a synthetic wheel + sdist holding the given entry names."""
    with zipfile.ZipFile(tmp / f"moneybird_mcp-{VERSION}-py3-none-any.whl", "w") as zf:
        for name in wheel:
            zf.writestr(name, "")
    with tarfile.open(tmp / f"moneybird_mcp-{VERSION}.tar.gz", "w:gz") as tf:
        for name in sdist:
            info = tarfile.TarInfo(name)
            info.size = 0
            tf.addfile(info, io.BytesIO(b""))
    return tmp


class DistHygieneTests(unittest.TestCase):
    def _check(self, wheel: list[str], sdist: list[str]) -> list[str]:
        with tempfile.TemporaryDirectory() as raw:
            dist = _build_dist(pathlib.Path(raw), wheel, sdist)
            return check_dist_hygiene.check(dist)

    def test_clean_artifacts_pass(self) -> None:
        self.assertEqual(self._check(CLEAN_WHEEL, CLEAN_SDIST), [])

    def test_sdist_may_ship_env_example(self) -> None:
        # .env.example holds no values and is included on purpose.
        problems = self._check(CLEAN_WHEEL, [*CLEAN_SDIST, f"moneybird_mcp-{VERSION}/.env.example"])
        self.assertEqual(problems, [])

    def test_wheel_rejects_entries_outside_the_package(self) -> None:
        # The sdist ships tests/ and docs/ on purpose; the wheel must not.
        problems = self._check([*CLEAN_WHEEL, "tests/test_x.py"], CLEAN_SDIST)
        self.assertEqual(len(problems), 1)
        self.assertIn("unexpected top-level entry", problems[0])
        self.assertIn("tests/test_x.py", problems[0])

    def test_packaged_secrets_are_flagged(self) -> None:
        for leaked in (
            "moneybird_mcp/.env",
            "moneybird_mcp/moneybird_oauth_tokens.json",
            "moneybird_mcp/moneybird_approvals.sqlite3",
            "moneybird_mcp/.moneybird_sync_index_123.json",
            "moneybird_mcp/.moneybird_search_fts_123.sqlite3",
            "moneybird_mcp/.moneybird_audit_log_123.jsonl",
            "moneybird_mcp/gateway_demo_users.json",
        ):
            with self.subTest(leaked=leaked):
                problems = self._check([*CLEAN_WHEEL, leaked], CLEAN_SDIST)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn("sensitive file", problems[0])
                self.assertIn(leaked, problems[0])

    def test_sdist_secrets_are_flagged(self) -> None:
        problems = self._check(
            CLEAN_WHEEL, [*CLEAN_SDIST, f"moneybird_mcp-{VERSION}/.moneybird_audit_log.jsonl"]
        )
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("sensitive file", problems[0])

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
