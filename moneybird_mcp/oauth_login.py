"""Interactive OAuth login against Moneybird (out-of-band flow).

Prerequisites: register an application at https://moneybird.com/user/applications/new
with redirect URI ``urn:ietf:wg:oauth:2.0:oob`` and supply its credentials through
the parent environment or an explicitly selected environment file. Then:

    python -m moneybird_mcp.oauth_login --env-file C:\\absolute\\operator.env

This lives in the installed package (not in ``scripts/``) so that the command works
for a ``pip install`` just as it does in a source checkout; the credential error
messages point at it. ``scripts/oauth_login.py`` delegates here.

The command prints the authorization URL; open it, click "Sta toe" (allow), copy the
code Moneybird displays, and paste it back here. Tokens are stored in the data dir
(moneybird_oauth_tokens.json) and picked up automatically when neither an
X-Moneybird-Token header nor MONEYBIRD_ACCESS_TOKEN is present.

Options:
    --redirect-uri URI   use a registered callback URL instead of out-of-band
    --profile NAME       store tokens under a non-default profile name
"""
from __future__ import annotations

import argparse
import os
import webbrowser
from pathlib import Path

from . import oauth
from .client import MoneybirdClient
from .config import load_env_file


def main() -> int:
    parser = argparse.ArgumentParser(prog="moneybird_mcp.oauth_login", description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        help=(
            "Explicit configuration file. Existing parent-process environment "
            "values win; no .env file is discovered automatically."
        ),
    )
    parser.add_argument("--redirect-uri", default=oauth.OOB_REDIRECT_URI)
    parser.add_argument("--profile", default=oauth.DEFAULT_PROFILE)
    args = parser.parse_args()
    if args.env_file is not None:
        load_env_file(args.env_file)
    if not os.environ.get("MONEYBIRD_MCP_DATA_DIR", "").strip():
        os.environ["MONEYBIRD_MCP_DATA_DIR"] = str(Path.home() / ".moneybird-mcp")

    url = oauth.build_authorize_url(redirect_uri=args.redirect_uri)
    print("Open this URL in your browser and authorize the application:\n")
    print(f"  {url}\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass  # printing the URL is enough; opening the browser is best-effort

    code = input("Paste the authorization code shown by Moneybird: ").strip()
    if not code:
        print("No code entered; aborting.")
        return 1

    tokens = oauth.exchange_authorization_code(code, redirect_uri=args.redirect_uri)
    oauth.store_tokens(tokens, profile=args.profile)
    print(f"\nTokens stored in {oauth.oauth_tokens_path()} (profile {args.profile!r}).")
    print(f"Granted scopes: {tokens.get('scope', '(not reported)')}")

    # Verify the token works by listing the administrations it can see.
    client = MoneybirdClient(
        token=tokens["access_token"],
        administration_id=None,
        require_administration=False,
    )
    administrations = client.list_administrations()
    print(f"\nToken verified — access to {len(administrations)} administration(s):")
    for administration in administrations:
        print(f"  - {administration.get('name')} (id {administration.get('id')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
