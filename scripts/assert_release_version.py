"""Fail when release metadata disagrees about the version being built."""
from __future__ import annotations

import argparse
import json
import tomllib
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("expected")
    args = parser.parse_args()

    pyproject_version = str(
        tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))["project"][
            "version"
        ]
    )
    manifest_version = str(
        json.loads(Path("mcpb/manifest.json").read_text(encoding="utf-8"))["version"]
    )
    actual = {
        "pyproject.toml": pyproject_version,
        "mcpb/manifest.json": manifest_version,
    }
    mismatches = {
        source: version
        for source, version in actual.items()
        if version != args.expected
    }
    if mismatches:
        raise SystemExit(
            f"Expected release version {args.expected}, but found {mismatches}"
        )
    print(f"Release metadata agrees on version {args.expected}")


if __name__ == "__main__":
    main()
