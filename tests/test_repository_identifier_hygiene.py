"""Keep real Moneybird record identifiers out of the repository.

Some fixtures were once written by copying identifiers straight out of a live
administration. Those ids are not secrets by themselves -- reading the records
behind them still needs a token -- but an administration id appears in every
moneybird.com URL, so publishing one ties this repository to a real business and
to whatever other ids were recorded beside it. The commits that introduced them
predate this guard; every value has since been replaced by a placeholder.

The check is deliberately an allowlist rather than a blocklist. Recognising "a
real id" requires a real id to compare against, which would put the very values
this guard exists to remove back into the source. Recognising "not one of our
placeholders" requires nothing. So every 18-digit number in a tracked file has
to match a documented placeholder shape, and anything else fails whether or not
this project has ever seen that administration.

Paths are checked as well as contents. This project's own working files are
named after the administration they came from -- sync caches, audit logs and
search indexes all carry the id in the filename -- so a committed dump would
publish an identifier in the file listing while its contents stayed clean.

Only tracked files are scanned. A developer checkout also holds ignored working
material -- synchronisation caches, audit logs, generated dumps -- which is full
of real ids by design and is never published.
"""
from __future__ import annotations

import pathlib
import re
import subprocess
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent

# A Moneybird id is an 18-digit integer. Require non-digits on both sides so a
# longer number is not sliced into something that merely looks like an id.
MONEYBIRD_ID_RE = re.compile(r"(?<![0-9])[0-9]{18}(?![0-9])")

# The placeholder shapes fixtures and tests are allowed to use. Extend this only
# with values that are obviously invented at a glance.
SYNTHETIC_ID_RE = re.compile(
    r"""^(?:
        ([0-9])\1{17}               # a single repeated digit: 111..., 999...
      | 123456789012345678          # the ascending sequence
      | 100000000000000[0-9]{3}     # the 1000...NNN placeholder series
    )$""",
    re.VERBOSE,
)

# Binary formats hold no hand-written fixtures and decode as noise.
SKIPPED_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".mcpb", ".zip", ".whl"}
)


def _tracked_files() -> list[pathlib.Path]:
    """Every file git tracks, or an empty list outside a work tree."""
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:
        return []
    if result.returncode != 0:
        return []
    names = result.stdout.decode("utf-8", "replace").split("\0")
    return [ROOT / name for name in names if name]


class RepositoryIdentifierHygieneTests(unittest.TestCase):
    def test_every_tracked_moneybird_id_is_a_documented_placeholder(self) -> None:
        tracked = _tracked_files()
        if not tracked:
            self.skipTest("no git work tree available; nothing to scan")

        offenders: list[str] = []
        for path in tracked:
            relative = path.relative_to(ROOT).as_posix()

            # A filename leaks as effectively as a line does, and it leaks from
            # a binary too, so the path is checked before anything is decoded.
            for found in MONEYBIRD_ID_RE.findall(relative):
                if not SYNTHETIC_ID_RE.match(found):
                    offenders.append(f"{relative}: {found} (in the path)")

            if path.suffix.lower() in SKIPPED_SUFFIXES or not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if not MONEYBIRD_ID_RE.search(text):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                for found in MONEYBIRD_ID_RE.findall(line):
                    if not SYNTHETIC_ID_RE.match(found):
                        offenders.append(f"{relative}:{number}: {found}")

        self.assertEqual(
            offenders,
            [],
            "Replace each identifier below with a placeholder from "
            "SYNTHETIC_ID_RE. A real id must not enter the repository even when "
            "the record it names is uninteresting:\n  " + "\n  ".join(offenders),
        )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
