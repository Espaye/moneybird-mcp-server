"""Assert the built wheel/sdist ship code only -- no credentials or local state.

This encodes the sanity check from docs/releasing.md step 3 so CI can enforce it:
the wheel must contain nothing but the `moneybird_mcp` package and its dist-info, and
neither artifact may contain a `.env`, OAuth tokens, the approvals database, a
sync cache, or an audit log.

Run after `python -m build`:

    python scripts/check_dist_hygiene.py            # inspects ./dist
    python scripts/check_dist_hygiene.py path/to/dist

Exits non-zero with one line per problem found.
"""

from __future__ import annotations

import pathlib
import sys
import tarfile
import zipfile

# Basenames (glob patterns) that must never be packaged. Mirrors the secret and
# per-administration state entries in .gitignore. `.env.example` is deliberately
# not matched -- it ships in the sdist on purpose and holds no values.
DENY_PATTERNS = (
    ".env",
    "moneybird_oauth_tokens.json",
    "moneybird_approvals.sqlite3",
    ".moneybird_sync_index*.json",
    ".moneybird_search_fts*.sqlite3",
    ".moneybird_audit_log*.jsonl",
    "gateway_demo_users.json",
)


def _sensitive(entry: str) -> bool:
    name = pathlib.PurePosixPath(entry).name
    return any(pathlib.PurePosixPath(name).match(pattern) for pattern in DENY_PATTERNS)


def check(dist_dir: pathlib.Path) -> list[str]:
    """Return a list of problems; empty means the artifacts are clean."""
    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        found = [p.name for p in (*wheels, *sdists)] or ["nothing"]
        return [
            f"expected exactly one wheel and one sdist in {dist_dir}, found: "
            + ", ".join(found)
        ]

    problems: list[str] = []
    with zipfile.ZipFile(wheels[0]) as wheel:
        wheel_entries = wheel.namelist()
    with tarfile.open(sdists[0]) as sdist:
        sdist_entries = sdist.getnames()

    # The wheel is the installed surface: package + metadata, nothing else.
    for entry in wheel_entries:
        top = entry.split("/", 1)[0]
        if top != "moneybird_mcp" and not top.endswith(".dist-info"):
            problems.append(f"{wheels[0].name}: unexpected top-level entry {entry!r}")

    for artifact, entries in ((wheels[0], wheel_entries), (sdists[0], sdist_entries)):
        for entry in entries:
            if _sensitive(entry):
                problems.append(f"{artifact.name}: sensitive file {entry!r}")

    if not problems:
        print(f"OK: {wheels[0].name} and {sdists[0].name} ship code only")
    return problems


def main(argv: list[str]) -> int:
    dist_dir = pathlib.Path(argv[1] if len(argv) > 1 else "dist")
    if not dist_dir.is_dir():
        print(f"no such directory: {dist_dir}", file=sys.stderr)
        return 2
    problems = check(dist_dir)
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
