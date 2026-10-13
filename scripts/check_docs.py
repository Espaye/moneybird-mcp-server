"""Offline Markdown links, GitHub-style anchors and declared source facts (stdlib only)."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def prose(text: str) -> str:
    """Mask fenced blocks without changing line numbers; examples are not navigation."""
    lines = []
    fence = None
    for line in text.splitlines():
        match = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if match:
            marker = match[1]
            if fence is None:
                fence = marker
            elif marker[0] == fence[0] and len(marker) >= len(fence):
                fence = None
            lines.append("")
        else:
            lines.append(line if fence is None else "")
    return "\n".join(lines)


def anchors(text: str) -> set[str]:
    text = prose(text)
    found = set(re.findall(r"""\b(?:id|name)=["']([^"']+)["']""", text))
    used: set[str] = set()
    lines = text.splitlines()
    headings = []
    for i, line in enumerate(lines):
        match = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        if match:
            headings.append(match[1])
        elif i and re.fullmatch(r" {0,3}(?:=+|-+)\s*", line) and lines[i - 1].strip():
            # Avoid table separators and list items.
            if "|" not in lines[i - 1] and not lines[i - 1].startswith("- "):
                headings.append(lines[i - 1])
    for heading in headings:
        heading = re.sub(r"<[^>]+>", "", heading)
        heading = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", heading)
        slug = "".join(c for c in heading.lower() if c in " -_" or unicodedata.category(c)[0] in "LN").replace(" ", "-")
        candidate = slug
        suffix = 0
        while candidate in used:
            suffix += 1
            candidate = f"{slug}-{suffix}"
        used.add(candidate)
        found.add(candidate)
    return found


def links(text: str) -> list[tuple[int, str]]:
    text = prose(text)
    text = re.sub(r"(`+)(.*?)\1", lambda m: " " * len(m[0]), text)
    definitions: dict[str, str] = {}
    for match in re.finditer(r"(?m)^ {0,3}\[([^\]]+)\]:\s*(?:<([^>]+)>|(\S+))", text):
        definitions[match[1].casefold()] = match[2] or match[3]
    result = []
    for match in re.finditer(r"!?\[[^\]\n]*(?:\][^\]\n]+)*\]\(\s*(?:<([^>]+)>|([^\s)]+))(?:\s+[^)]*)?\)", text):
        result.append((text.count("\n", 0, match.start()) + 1, match[1] or match[2]))
    for match in re.finditer(r"(?m)^ {0,3}\[[^\]]+\]:\s*(?:<([^>]+)>|(\S+))", text):
        result.append((text.count("\n", 0, match.start()) + 1, match[1] or match[2]))
    for match in re.finditer(r"""<(?:a|img)\b[^>]*?\b(?:href|src)=["']([^"']+)["']""", text):
        result.append((text.count("\n", 0, match.start()) + 1, match[1]))
    # Reference uses, including collapsed forms. Undefined explicit references fail.
    for match in re.finditer(r"\[([^\]\n]+)\]\[([^\]\n]*)\]", text):
        key = (match[2] or match[1]).casefold()
        result.append((text.count("\n", 0, match.start()) + 1, definitions.get(key, f"!undefined:{key}")))
    return result


def document_errors(root: Path, peers: dict[str, Path] | None = None) -> list[str]:
    """Tracked Markdown plus new files; never traverse venvs, caches or junctions."""
    config_path = root / "docs/checks.json"
    config = json.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
    repository = config.get("repository", "")
    peers = {repository: root, **(peers or {})}
    git = shutil.which("git")
    if git is None:
        raise RuntimeError("Git is required to inventory tracked Markdown")
    names = subprocess.check_output(  # noqa: S603 - resolved Git, fixed read-only arguments
        [git, "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.md"], cwd=root, text=True
    ).splitlines()
    errors = []
    cache: dict[Path, set[str]] = {}
    for name in sorted(set(names)):
        path = root / name
        if not path.is_file():
            continue  # staged rename: old index path no longer exists
        for line, target in links(path.read_text(encoding="utf-8")):
            if target.startswith("!undefined:"):
                errors.append(f"{name}:{line}: {target}")
                continue
            parts = urlsplit(target)
            base = root
            if parts.scheme or parts.netloc:
                match = re.fullmatch(r"/([^/]+/[^/]+)/blob/main/(.+)", parts.path)
                if parts.netloc != "github.com" or not match or match[1] not in peers:
                    continue  # no network crawl or historical-SHA rewriting
                base = peers[match[1]]
                resolved = base / unquote(match[2])
            elif parts.path:
                resolved = (
                    root / unquote(parts.path.lstrip("/"))
                    if parts.path.startswith("/")
                    else path.parent / unquote(parts.path)
                )
            else:
                resolved = path
            resolved = resolved.resolve()
            if not resolved.is_relative_to(base.resolve()):
                errors.append(f"{name}:{line}: outside repository: {target}")
                continue
            if not resolved.exists():
                errors.append(f"{name}:{line}: missing file: {target}")
                continue
            if parts.fragment and resolved.suffix.lower() == ".md":
                if resolved not in cache:
                    cache[resolved] = anchors(resolved.read_text(encoding="utf-8"))
                if unquote(parts.fragment) not in cache[resolved]:
                    errors.append(f"{name}:{line}: missing anchor: {target}")
    coverage = config.get("api_coverage")
    if coverage:
        snapshot = json.loads((root / coverage["snapshot"]).read_text(encoding="utf-8"))["paths"]
        expected = {
            (method, path.removeprefix("/{administration_id}"))
            for path, methods in snapshot.items()
            for method in methods
        }
        document = (root / coverage["document"]).read_text(encoding="utf-8")
        rows = re.findall(r"^\| (GET|POST|PATCH|DELETE) \| `([^`]+)` \|.*? \| (.+) \|$", document, re.MULTILINE)
        actual = {(method, path) for method, path, _ in rows}
        if actual != expected or len(rows) != len(expected):
            errors.append(
                f"{coverage['document']}: endpoint inventory drift: "
                f"missing={sorted(expected - actual)}, extra={sorted(actual - expected)}"
            )
        for symbol, pattern in coverage["totals"].items():
            count = sum(status.startswith(symbol) for _, _, status in rows)
            if re.findall(pattern, document) != [str(count)]:
                errors.append(f"{coverage['document']}: coverage total drift for {symbol}: expected {count}")
    for fact in config.get("facts", []):
        source_path = root / fact["source"]
        if "pointer" in fact:
            value = json.loads(source_path.read_text(encoding="utf-8"))
            for key in fact["pointer"].split("/"):
                if key:
                    value = value[int(key)] if isinstance(value, list) else value[key]
            if "filter" in fact:
                field, expected = fact["filter"]
                value = [item for item in value if item[field] == expected]
            if fact.get("sum_lengths"):
                value = sum(len(item) for item in value.values())
            elif fact.get("length"):
                value = len(value)
        else:
            matches = re.findall(fact["source_pattern"], source_path.read_text(encoding="utf-8"), re.MULTILINE)
            if len(matches) != 1:
                errors.append(f"{fact['source']}: expected one source fact, got {len(matches)}")
                continue
            value = matches[0]
        document = (root / fact["document"]).read_text(encoding="utf-8")
        actual = re.findall(fact["pattern"], document, re.MULTILINE)
        if actual != [str(value)]:
            errors.append(f"{fact['document']}: source fact drift: expected {value!s}, got {actual!r}")
    return errors


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--peer",
        action="append",
        default=[],
        metavar="OWNER/REPO=PATH",
        help="Check cross-repository main links against an isolated peer checkout",
    )
    args = parser.parse_args()
    peers = {}
    for argument in args.peer:
        name, separator, path = argument.partition("=")
        if not separator or not Path(path).is_dir():
            parser.error(f"invalid peer: {argument}")
        peers[name] = Path(path).resolve()
    errors = document_errors(ROOT, peers)
    for error in errors:
        print(error)
    if errors:
        raise SystemExit(f"Documentation checks failed: {len(errors)} issue(s)")
    print("Documentation links, anchors and declared source facts pass.")


if __name__ == "__main__":
    main()
