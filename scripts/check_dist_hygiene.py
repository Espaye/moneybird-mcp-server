"""Fail closed when a built wheel or sdist ships an unapproved path or private data.

Run after ``python -m build``::

    python scripts/check_dist_hygiene.py            # inspects ./dist
    python scripts/check_dist_hygiene.py path/to/dist

Two independent policies apply. Paths are an allowlist: an artifact may contain
only the files this project deliberately publishes, so a stray dump is rejected
without anyone having to predict it. Content is scanned for the *shapes* private
data takes -- record identifiers, bank account numbers, real e-mail addresses,
registration numbers, absolute developer paths -- rather than for a list of known
values.

That distinction is the point. A gate that recognises private data by comparing
against real examples has to carry those examples, which publishes them in the
very file meant to keep them out; this script therefore contains no real
identifier, name, account number or document reference, and it never should. A
structural rule needs no example: it rejects an identifier this project has never
seen just as readily as one it has.

Some values genuinely cannot be recognised by shape -- a personal name is just a
word. Set ``MONEYBIRD_HYGIENE_MARKERS_FILE`` to a JSON file **outside the
repository** to add literal substrings for those. The variable is optional and the
structural checks above stand on their own without it; when it is set the file
must load, so a misconfigured path fails the gate instead of silently weakening it.

Exits non-zero with one line per problem found.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tarfile
import zipfile
from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Path policy: exactly what this project publishes.
# ---------------------------------------------------------------------------

PUBLIC_ROOT_FILES = frozenset(
    {
        ".env.example",
        ".gitignore",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "LICENSE",
        "PKG-INFO",
        "README.md",
        "README.nl.md",
        "SECURITY.md",
        "SUPPORT.md",
        "moneybird_mcp_server.py",
        "pyproject.toml",
        "requirements-minimum.txt",
        "requirements.txt",
    }
)

PUBLIC_DOCS = frozenset(
    {
        "docs/data-lifecycle.md",
        "docs/data-lifecycle.nl.md",
        "docs/data_handling.md",
        "docs/deployment-and-safety.md",
        "docs/getting-started.md",
        "docs/getting-started.nl.md",
        "docs/moneybird_api_coverage.md",
        "docs/moneybird_api_paths.json",
        "docs/moneybird_api_scopes.json",
        "docs/oauth.md",
        "docs/reading_pdf_attachments.md",
        "docs/releasing.md",
        "docs/threat_model.md",
        "docs/tool-reference.md",
        "docs/workflow-catalogue.md",
    }
)

PUBLIC_SCRIPTS = frozenset(
    {
        "scripts/assert_release_version.py",
        "scripts/build_mcpb.py",
        "scripts/build_sbom.py",
        "scripts/check_dist_hygiene.py",
        "scripts/check_reproducible_build.py",
        "scripts/healthcheck_readonly.py",
        "scripts/oauth_login.py",
        "scripts/reconcile_execution.py",
        "scripts/render_api_scopes.py",
        "scripts/render_workflow_catalogue.py",
        "scripts/smoke_dist_install.py",
    }
)

PUBLIC_MCPB_FILES = frozenset({"mcpb/main.py", "mcpb/manifest.json"})

PUBLIC_TEST_DATA = frozenset(
    {"tests/fixtures/moneybird/product_response_v2_20260804.json"}
)

# Basenames (glob patterns) that must never be packaged, mirroring the secret and
# per-administration state entries in .gitignore. The path allowlist above already
# rejects every one of these; naming them separately turns "unexpected path" into
# "you packaged the token store", which is the difference between a puzzle and an
# instruction. `.env.example` is deliberately not matched -- it ships on purpose
# and holds no values.
DENY_PATTERNS = (
    ".env",
    "moneybird_oauth_tokens.json",
    "moneybird_approvals.sqlite3",
    ".moneybird_sync_index*.json",
    ".moneybird_search_fts*.sqlite3",
    ".moneybird_audit_log*.jsonl",
)


def _sensitive(entry: str) -> bool:
    name = pathlib.PurePosixPath(entry).name
    return any(pathlib.PurePosixPath(name).match(pattern) for pattern in DENY_PATTERNS)

# ---------------------------------------------------------------------------
# Structural content policy.
# ---------------------------------------------------------------------------

# A Moneybird id is an 18-digit integer. Require non-digits on both sides so a
# longer number is not sliced into something that merely looks like an id.
_MONEYBIRD_ID_RE = re.compile(rb"(?<![0-9])[0-9]{18}(?![0-9])")

# The placeholder shapes tests and fixtures may use. Anything else is treated as
# a real identifier, whether or not this project has ever seen that record. Kept
# in step with tests/test_repository_identifier_hygiene.py, which applies the
# same shapes to the repository itself.
_SYNTHETIC_ID_RE = re.compile(
    rb"""^(?:
        ([0-9])\1{17}               # a single repeated digit: 111..., 999...
      | 123456789012345678          # the ascending sequence
      | 100000000000000[0-9]{3}     # the 1000...NNN placeholder series
    )$""",
    re.VERBOSE,
)

# IBANs are 15-34 characters: two letters, two check digits, then the national
# part. Demand delimiters so a longer alphanumeric run is not sliced, and demand
# digits in the national part so an ordinary uppercase word cannot reach the
# checksum test by accident.
_IBAN_CANDIDATE_RE = re.compile(
    rb"(?<![A-Z0-9])([A-Z]{2}[0-9]{2}[A-Z0-9]{11,30})(?![A-Z0-9])"
)
_IBAN_MINIMUM_DIGITS = 4

# Domains reserved by RFC 2606/6761 for documentation and testing. Every address
# this project publishes uses one, so the rule needs no list of real addresses --
# and adding a real one to an allowlist would defeat the purpose. A deliverable
# address always ends in a dotted top-level domain of at least two letters, so
# requiring one keeps placeholders like `a@b.c` out of the results without
# excusing anything a message could actually be sent to.
_EMAIL_RE = re.compile(
    rb"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})"
)
_RESERVED_EMAIL_DOMAINS = ("example.com", "example.net", "example.org")
_RESERVED_EMAIL_SUFFIXES = (".example", ".test", ".invalid", ".localhost")

# Registration numbers are only recognisable next to the field that names them:
# eight bare digits are not evidence of anything, but a populated
# `chamber_of_commerce` is. Matching the key avoids guessing.
_LABELLED_REGISTRATION_RE = re.compile(
    rb"\"(chamber_of_commerce|tax_number)\"\s*:\s*\"([^\"]+)\""
)

_WINDOWS_ABSOLUTE_PATH_RE = re.compile(
    rb"(?<![A-Za-z])[A-Za-z]:(?:[\\]+|[/]+)[^\x00\r\n\t \"'<>|`]+"
)
# Invented paths that documentation and tests use to show the shape of an
# operator-supplied location. They name no real machine or account.
_SAFE_WINDOWS_PATHS = frozenset(
    {
        b"c:\\absolute\\operator.env",
        b"c:\\users\\runner~1",
    }
)

MARKERS_FILE_ENV = "MONEYBIRD_HYGIENE_MARKERS_FILE"


class MarkerConfigurationError(RuntimeError):
    """The operator asked for extra markers and they could not be loaded."""


def load_extra_markers() -> tuple[bytes, ...]:
    """Literal substrings from ``MONEYBIRD_HYGIENE_MARKERS_FILE``, if set.

    Returns an empty tuple when the variable is absent: the structural checks are
    the gate, and these markers only add the values no shape can describe. When
    the variable *is* set, every failure is fatal -- a typo in the path must not
    quietly turn the extra layer off.
    """
    configured = os.environ.get(MARKERS_FILE_ENV, "").strip()
    if not configured:
        return ()
    path = pathlib.Path(configured)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MarkerConfigurationError(f"{MARKERS_FILE_ENV}={configured!r}: {exc}") from exc
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MarkerConfigurationError(
            f"{MARKERS_FILE_ENV}={configured!r}: not valid JSON: {exc}"
        ) from exc
    if not isinstance(document, dict):
        raise MarkerConfigurationError(
            f"{MARKERS_FILE_ENV}={configured!r}: expected a JSON object"
        )
    literals = document.get("literals", [])
    if not isinstance(literals, list) or any(not isinstance(v, str) for v in literals):
        raise MarkerConfigurationError(
            f"{MARKERS_FILE_ENV}={configured!r}: 'literals' must be a list of strings"
        )
    return tuple(value.lower().encode("utf-8") for value in literals if value)


def _valid_archive_path(entry: str) -> bool:
    path = pathlib.PurePosixPath(entry)
    return (
        bool(entry)
        and "\\" not in entry
        and not path.is_absolute()
        and ".." not in path.parts
    )


def _sdist_relative_path(entry: str) -> str | None:
    if not _valid_archive_path(entry):
        return None
    parts = pathlib.PurePosixPath(entry).parts
    if len(parts) < 2 or not parts[0].startswith("moneybird_mcp-"):
        return None
    return pathlib.PurePosixPath(*parts[1:]).as_posix()


def _allowed_wheel_path(entry: str) -> bool:
    if not _valid_archive_path(entry):
        return False
    path = pathlib.PurePosixPath(entry)
    top = path.parts[0]
    if top.endswith(".dist-info"):
        return True
    return top == "moneybird_mcp" and path.suffix in {".py", ".md"}


def _allowed_sdist_path(relative: str) -> bool:
    if relative in PUBLIC_ROOT_FILES | PUBLIC_DOCS | PUBLIC_SCRIPTS:
        return True
    if relative in PUBLIC_MCPB_FILES | PUBLIC_TEST_DATA:
        return True
    path = pathlib.PurePosixPath(relative)
    if relative.startswith("moneybird_mcp/"):
        return path.suffix in {".py", ".md"}
    return len(path.parts) == 2 and path.parts[0] == "tests" and path.suffix == ".py"


def _normalise_windows_path(value: bytes) -> bytes:
    value = value.lower().rstrip(b".,;:!?)])}")
    return re.sub(rb"[\\/]+", rb"\\", value)


def _iban_checksum_holds(candidate: bytes) -> bool:
    """True when the candidate satisfies the ISO 13616 mod-97 check."""
    if sum(byte in b"0123456789" for byte in candidate) < _IBAN_MINIMUM_DIGITS:
        return False
    rotated = candidate[4:] + candidate[:4]
    digits = []
    for byte in rotated:
        character = chr(byte)
        if character.isdigit():
            digits.append(character)
        elif character.isalpha():
            digits.append(str(ord(character.upper()) - ord("A") + 10))
        else:  # pragma: no cover - the pattern admits no other byte
            return False
    return int("".join(digits)) % 97 == 1


def _reserved_email_domain(domain: bytes) -> bool:
    text = domain.lower().rstrip(b".").decode("ascii", "replace")
    if text in _RESERVED_EMAIL_DOMAINS:
        return True
    return any(text.endswith(suffix) for suffix in _RESERVED_EMAIL_SUFFIXES)


def _identifier_problems(subject: str, data: bytes) -> list[str]:
    """Structural findings in one blob of packaged bytes."""
    problems: list[str] = []

    unexpected = {
        match.group()
        for match in _MONEYBIRD_ID_RE.finditer(data)
        if not _SYNTHETIC_ID_RE.match(match.group())
    }
    if unexpected:
        problems.append(
            f"{subject}: {len(unexpected)} identifier(s) outside the documented "
            "synthetic placeholder shapes"
        )

    ibans = {
        match.group(1)
        for match in _IBAN_CANDIDATE_RE.finditer(data)
        if _iban_checksum_holds(match.group(1))
    }
    if ibans:
        problems.append(f"{subject}: {len(ibans)} checksum-valid bank account number(s)")

    addresses = {
        match.group()
        for match in _EMAIL_RE.finditer(data)
        if not _reserved_email_domain(match.group(1))
    }
    if addresses:
        problems.append(
            f"{subject}: {len(addresses)} e-mail address(es) outside the reserved "
            "documentation domains"
        )

    registrations = {
        match.group(1)
        for match in _LABELLED_REGISTRATION_RE.finditer(data)
        if match.group(2).strip()
    }
    if registrations:
        problems.append(
            f"{subject}: populated registration field(s): "
            + ", ".join(sorted(field.decode("ascii", "replace") for field in registrations))
        )

    for match in _WINDOWS_ABSOLUTE_PATH_RE.finditer(data):
        if _normalise_windows_path(match.group()) not in _SAFE_WINDOWS_PATHS:
            problems.append(f"{subject}: an absolute Windows path")
            break

    return problems


def _content_problems(
    artifact_name: str, entry: str, data: bytes, extra_markers: tuple[bytes, ...]
) -> list[str]:
    subject = f"{artifact_name}: {entry!r} contains"
    problems = _identifier_problems(subject, data)

    # A packaged name leaks as effectively as packaged content, and it leaks from
    # a binary too, so the archive path is scanned as well.
    problems.extend(_identifier_problems(f"{artifact_name}: the path {entry!r} contains", entry.encode()))

    lowered = data.lower()
    hits = sum(marker in lowered for marker in extra_markers)
    if hits:
        problems.append(f"{subject} {hits} configured private marker(s)")
    return problems


def _check_wheel(path: pathlib.Path, extra_markers: tuple[bytes, ...]) -> list[str]:
    problems: list[str] = []
    with zipfile.ZipFile(path) as wheel:
        for info in wheel.infolist():
            if info.is_dir():
                continue
            # A known secret is named as such; the generic path rule would only
            # say the entry was unexpected.
            if _sensitive(info.filename):
                problems.append(f"{path.name}: sensitive file {info.filename!r}")
                continue
            if not _allowed_wheel_path(info.filename):
                problems.append(f"{path.name}: unexpected wheel path {info.filename!r}")
            problems.extend(
                _content_problems(path.name, info.filename, wheel.read(info), extra_markers)
            )
    return problems


def _check_sdist(path: pathlib.Path, extra_markers: tuple[bytes, ...]) -> list[str]:
    problems: list[str] = []
    with tarfile.open(path) as sdist:
        for member in sdist.getmembers():
            if not member.isfile():
                continue
            if _sensitive(member.name):
                problems.append(f"{path.name}: sensitive file {member.name!r}")
                continue
            relative = _sdist_relative_path(member.name)
            if relative is None or not _allowed_sdist_path(relative):
                problems.append(f"{path.name}: unexpected sdist path {member.name!r}")
            extracted = sdist.extractfile(member)
            if extracted is None:
                problems.append(f"{path.name}: could not inspect {member.name!r}")
                continue
            problems.extend(
                _content_problems(path.name, member.name, extracted.read(), extra_markers)
            )
    return problems


def check(dist_dir: pathlib.Path) -> list[str]:
    """Return all path/content problems; an empty list means both artifacts are clean."""
    try:
        extra_markers = load_extra_markers()
    except MarkerConfigurationError as exc:
        return [str(exc)]

    wheels = sorted(dist_dir.glob("*.whl"))
    sdists = sorted(dist_dir.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        found: Iterable[pathlib.Path] = (*wheels, *sdists)
        names = [path.name for path in found] or ["nothing"]
        return [
            f"expected exactly one wheel and one sdist in {dist_dir}, found: "
            + ", ".join(names)
        ]

    problems = [
        *_check_wheel(wheels[0], extra_markers),
        *_check_sdist(sdists[0], extra_markers),
    ]
    if not problems:
        source = f" plus {len(extra_markers)} configured marker(s)" if extra_markers else ""
        print(
            f"OK: {wheels[0].name} and {sdists[0].name} contain only approved "
            f"paths and structurally private-data-free content{source}"
        )
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
