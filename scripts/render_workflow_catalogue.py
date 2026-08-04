"""Render or verify the checked-in workflow catalogue."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGET = ROOT / "docs" / "workflow-catalogue.md"
sys.path.insert(0, str(ROOT))

from moneybird_mcp.workflow_catalogue import (  # noqa: E402
    render_workflow_catalogue_markdown,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the checked-in catalogue differs from the typed registry.",
    )
    args = parser.parse_args()
    expected = render_workflow_catalogue_markdown()
    if args.check:
        actual = TARGET.read_text(encoding="utf-8")
        if actual != expected:
            raise SystemExit(
                "docs/workflow-catalogue.md is stale; run "
                "python scripts/render_workflow_catalogue.py"
            )
        return 0
    TARGET.write_text(expected, encoding="utf-8", newline="\n")
    print(f"Rendered {TARGET.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
