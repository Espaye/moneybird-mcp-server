"""Interactive OAuth login against Moneybird — source-checkout wrapper.

The supported command for an installed package is::

    moneybird-mcp auth login --env-file C:\\absolute\\operator.env

This wrapper keeps the documented checkout command working without an install:

    python scripts/oauth_login.py --env-file C:\\absolute\\operator.env

The implementation lives in :mod:`moneybird_mcp.auth_cli` so that it ships inside
the wheel; ``scripts/`` is not part of it.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moneybird_mcp.oauth_login import main  # noqa: E402  (after sys.path setup)

if __name__ == "__main__":
    raise SystemExit(main())
