"""Interactive OAuth login against Moneybird — compatibility entrypoint.

The supported command is now::

    moneybird-mcp auth login --env-file C:\\absolute\\operator.env

``python -m moneybird_mcp.oauth_login`` keeps working and is exactly that
command: existing documentation, shell history and the ``scripts/oauth_login.py``
checkout wrapper all point here, and breaking them would strand users mid-setup
for no gain. It accepts the same options as ``auth login``.

The implementation lives in :mod:`moneybird_mcp.auth_cli`.
"""
from __future__ import annotations

import sys

from . import auth_cli


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Resolved through the module, not bound at import, so this stays a genuine
    # alias for whatever `auth login` currently does.
    return auth_cli.main(["login", *arguments])


if __name__ == "__main__":
    raise SystemExit(main())
