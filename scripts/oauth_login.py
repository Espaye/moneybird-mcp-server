"""Interactive OAuth login against Moneybird — source-checkout wrapper.

The implementation lives in :mod:`moneybird_mcp.oauth_login` so that it ships inside
the wheel; a ``pip install`` user runs ``python -m moneybird_mcp.oauth_login``. This
wrapper keeps the documented checkout command working:

    python scripts/oauth_login.py --env-file C:\\absolute\\operator.env
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moneybird_mcp.oauth_login import main  # noqa: E402  (after sys.path setup)

if __name__ == "__main__":
    raise SystemExit(main())
