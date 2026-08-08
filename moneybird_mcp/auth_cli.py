"""``moneybird-mcp auth`` — connect this installation to a Moneybird account.

Three commands:

    moneybird-mcp auth login    [--env-file PATH] [--profile NAME] [...]
    moneybird-mcp auth status   [--env-file PATH] [--profile NAME]
    moneybird-mcp auth logout   [--profile NAME]

``login`` runs Moneybird's out-of-band authorization-code flow: it prints (and
optionally opens) the consent URL, takes the code Moneybird displays in the
browser, exchanges it for tokens, verifies them, and stores the connection.
The out-of-band redirect is a local/development mechanism; a hosted product
registers an HTTPS callback instead and reuses the same
:mod:`moneybird_mcp.oauth` layer underneath.

This module is presentation only. It formats and prompts; it constructs no URLs,
parses no responses, and touches no files itself. Nothing it prints contains a
client secret, an authorization code, an access token, or a refresh token.
"""
from __future__ import annotations

import argparse
import os
import sys
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import oauth
from .config import MoneybirdError, load_env_file
from .oauth_scopes import (
    CAPABILITY_SCOPES,
    INCIDENTAL_ACCESS,
    SCOPE_PROFILES,
    SCOPES_ENV,
    format_scopes,
    missing_scopes,
    parse_scopes,
    unavailable_areas,
)
from .oauth_store import DEFAULT_PROFILE

PROG = "moneybird-mcp auth"


def _out(message: str = "") -> None:
    print(message)


def _err(message: str) -> None:
    print(message, file=sys.stderr)


def _default_data_dir() -> None:
    """Match the console script's stdio default so the server finds the tokens.

    ``moneybird-mcp`` on stdio defaults its state root to ``~/.moneybird-mcp``.
    Logging in against a different directory would store a working connection
    somewhere the server never looks, which presents as "I just logged in and it
    still says no credentials".
    """
    if not os.environ.get("MONEYBIRD_MCP_DATA_DIR", "").strip():
        os.environ["MONEYBIRD_MCP_DATA_DIR"] = str(Path.home() / ".moneybird-mcp")


def _timestamp(value: int | None) -> str:
    if not value:
        return "unknown"
    try:
        return datetime.fromtimestamp(int(value), tz=timezone.utc).isoformat(
            timespec="seconds"
        )
    except (OverflowError, OSError, ValueError):
        return "unknown"


def _administration_label(administration: dict[str, Any]) -> str:
    name = administration.get("name") or "unnamed"
    return f"{name} (id {administration.get('id')})"


def _select_administration(
    administrations: list[dict[str, Any]],
    *,
    requested: str,
    interactive: bool,
) -> str | None:
    """Decide which administration this connection should use.

    Returns ``None`` when the choice cannot be made without guessing, which is
    not a failure: the connection is already stored and usable, and the id can
    be supplied later through ``MONEYBIRD_ADMINISTRATION_ID`` or another login.
    Picking one silently is the outcome to avoid — every subsequent write would
    land in an administration the user never chose.
    """
    available = {str(item.get("id")): item for item in administrations}

    if requested:
        if requested not in available:
            raise MoneybirdError(
                f"Administration {requested!r} is not accessible with this "
                "connection. Available: "
                + ", ".join(_administration_label(item) for item in administrations)
            )
        return requested

    if not administrations:
        _out(
            "\nWarning: this connection can reach no administrations. Check the "
            "Moneybird account you authorized."
        )
        return None

    if len(administrations) == 1:
        only = str(administrations[0].get("id"))
        _out(f"\nSelected the only available administration: "
             f"{_administration_label(administrations[0])}")
        return only

    _out("\nThis connection can reach several administrations:")
    for index, item in enumerate(administrations, start=1):
        _out(f"  {index}. {_administration_label(item)}")

    if not interactive:
        _out(
            "\nNo administration was selected (not running interactively). "
            "Re-run with --administration ID, or set MONEYBIRD_ADMINISTRATION_ID "
            "in the environment your MCP client starts the server with."
        )
        return None

    while True:
        answer = input(
            "\nSelect an administration by number (or press Enter to skip): "
        ).strip()
        if not answer:
            _out(
                "Skipped. Set MONEYBIRD_ADMINISTRATION_ID, or run "
                f"'{PROG} login --administration ID' later."
            )
            return None
        if answer.isdigit() and 1 <= int(answer) <= len(administrations):
            return str(administrations[int(answer) - 1].get("id"))
        if answer in available:
            return answer
        _out("Not one of the options above.")


def _verify_connection(access_token: str) -> list[dict[str, Any]]:
    """Prove the new token works by listing the administrations it can reach.

    The response is normalised rather than trusted. This runs immediately after
    the authorization code has been spent, so an unexpected payload shape must
    surface as a message the user can act on, not as a traceback over a
    connection that was in fact stored successfully.
    """
    from .client import MoneybirdClient

    client = MoneybirdClient(
        token=access_token,
        administration_id=None,
        require_administration=False,
    )
    administrations = client.list_administrations()
    if not isinstance(administrations, list):
        raise MoneybirdError(
            "Moneybird returned an unexpected response when listing "
            "administrations."
        )
    return [
        item
        for item in administrations
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    ]


def command_login(args: argparse.Namespace) -> int:
    from .credentials import CREDENTIAL_MODE_HOSTED_REQUEST_ONLY, get_credential_mode

    scopes = parse_scopes(args.scopes or os.environ.get(SCOPES_ENV, ""))
    scope_value = format_scopes(scopes)

    # Said before the browser opens, not after: hosted request mode reads
    # credentials only from the gateway, so a login there is wasted effort and
    # a spent authorization.
    if get_credential_mode() == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        _out(
            "Note: this environment sets credential mode "
            f"{CREDENTIAL_MODE_HOSTED_REQUEST_ONLY!r}, which takes credentials "
            "only from\nthe trusted gateway. A connection stored here will not "
            "be used by that server.\n"
        )

    # Fails here, before the browser opens, when the application credentials are
    # missing — rather than after the user has already authorized.
    oauth.oauth_client_config()

    url = oauth.build_authorize_url(redirect_uri=args.redirect_uri, scope=scope_value)
    _out("Connecting this installation to Moneybird.\n")
    _out(f"Requesting scopes: {scope_value}")
    _out("\nOpen this URL in your browser and authorize the application:\n")
    _out(f"  {url}\n")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - opening a browser is best-effort
            pass  # printing the URL is enough, and headless hosts have none

    if args.redirect_uri == oauth.OOB_REDIRECT_URI:
        _out("Moneybird will show a short authorization code after you approve.")
        prompt = "Paste the authorization code shown by Moneybird: "
    else:
        prompt = "Paste the full callback URL you were redirected to: "

    try:
        answer = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        _err("\nAborted; nothing was stored.")
        return 1
    if not answer:
        _err("No authorization code entered; nothing was stored.")
        return 1

    if args.redirect_uri == oauth.OOB_REDIRECT_URI:
        code = answer
    else:
        code = oauth.parse_authorization_callback(answer)

    tokens = oauth.exchange_authorization_code(code, redirect_uri=args.redirect_uri)

    # Persist before verifying. The grant has already been consumed at this
    # point, so discarding it because a follow-up read failed would cost the
    # user another authorization round trip for no safety gain.
    connection = oauth.store_tokens(tokens, profile=args.profile)

    _out(
        f"\nConnection stored in {oauth.credential_location()} "
        f"(profile {args.profile!r})."
    )
    _out(f"Granted scopes: {connection.scope or '(not reported by Moneybird)'}")
    if connection.expires_in:
        _out(f"Access token expires at: {_timestamp(connection.expires_at)}")
        _out(f"Refresh token stored: {'yes' if connection.refresh_token else 'no'}")
    else:
        _out(
            "Moneybird reported no expiry for this access token. A refresh "
            f"token is {'stored' if connection.refresh_token else 'not stored'} "
            "for when that changes."
        )

    if connection.scope:
        absent = missing_scopes(connection.scope, scopes)
        if absent:
            _out(
                "\nWarning: Moneybird did not grant "
                f"{', '.join(absent)}. Tools that need those scopes will fail "
                "with an authorization error."
            )

    try:
        administrations = _verify_connection(connection.access_token)
    except MoneybirdError as exc:
        _err(
            f"\nThe connection was stored, but verifying it failed: {exc}\n"
            f"Run '{PROG} status' once the problem is resolved."
        )
        return 1

    # The count only; _select_administration names or lists them, so printing
    # both would show the same list twice.
    _out(
        f"\nVerified: the connection can reach "
        f"{len(administrations)} administration(s)."
    )

    selected = _select_administration(
        administrations,
        requested=(args.administration or "").strip(),
        interactive=sys.stdin.isatty(),
    )
    if selected:
        oauth.save_connection(
            connection.with_administration(selected), profile=args.profile
        )
        _out(f"\nAdministration {selected} saved for this connection.")

    _out("\nDone. Start your MCP client normally; no token needs to be copied.")
    return 0


def command_status(args: argparse.Namespace) -> int:
    """Report which Moneybird identity would be used, and from where.

    Prints no token, no secret, and no fingerprint of either. The questions this
    has to answer are "is something configured", "which source wins", and "which
    administration" — none of which require revealing a credential.
    """
    from .credentials import (
        CREDENTIAL_MODE_ENV,
        CREDENTIAL_MODE_HOSTED_REQUEST_ONLY,
        get_credential_mode,
    )

    mode = get_credential_mode()
    _out(f"Credential mode:        {mode}  ({CREDENTIAL_MODE_ENV})")
    _out(f"Credential store:       {oauth.credential_location()}")
    if mode == CREDENTIAL_MODE_HOSTED_REQUEST_ONLY:
        # Otherwise a stored connection listed below reads as the one in use,
        # and the real answer to "why is my login ignored?" is invisible.
        _out(
            "  note:                 this mode takes credentials only from the "
            "trusted gateway;\n                        anything stored locally "
            "is never read."
        )

    client_id = os.environ.get(oauth.CLIENT_ID_ENV, "").strip()
    has_secret = bool(os.environ.get(oauth.CLIENT_SECRET_ENV, "").strip())
    _out(
        f"OAuth application:      "
        f"{'client id ' + client_id if client_id else 'client id not set'}"
        f", secret {'set' if has_secret else 'not set'}"
    )

    env_token = bool(os.environ.get("MONEYBIRD_ACCESS_TOKEN", "").strip())
    env_administration = os.environ.get("MONEYBIRD_ADMINISTRATION_ID", "").strip()

    try:
        connection = oauth.load_connection(args.profile)
    except MoneybirdError as exc:
        _err(f"\nThe stored connection could not be read: {exc}")
        return 1

    _out("")
    if env_token:
        _out("Personal API token:     set (MONEYBIRD_ACCESS_TOKEN)")
    else:
        _out("Personal API token:     not set")

    if connection is None:
        _out(f"OAuth connection:       none stored for profile {args.profile!r}")
    else:
        summary = connection.describe()
        _out(f"OAuth connection:       stored for profile {args.profile!r}")
        _out(f"  scopes granted:       {summary['scope'] or '(not reported)'}")
        _out(f"  obtained at:          {_timestamp(summary['obtained_at'])}")
        if summary["expires_in"]:
            expired = connection.is_expired()
            _out(
                f"  access token expiry:  {_timestamp(summary['expires_at'])}"
                f"{'  (EXPIRED)' if expired else ''}"
            )
        else:
            _out("  access token expiry:  none reported by Moneybird")
        _out(f"  refresh token:        {'stored' if summary['has_refresh_token'] else 'absent'}")
        _out(f"  administration:       {summary['administration_id'] or 'not selected'}")

    # State the winner explicitly. An installation with both a personal token
    # and an OAuth connection is exactly where "which Moneybird identity am I
    # actually acting as" stops being obvious.
    _out("")
    if env_token:
        _out(
            "Active identity:        the personal API token in "
            "MONEYBIRD_ACCESS_TOKEN (it takes precedence over the OAuth "
            "connection)."
        )
    elif connection is not None:
        administration = env_administration or connection.administration_id
        _out(
            "Active identity:        the stored OAuth connection, "
            f"administration {administration or 'auto-selected at first call'}."
        )
        if env_administration and connection.administration_id and (
            env_administration != connection.administration_id
        ):
            _out(
                f"  note:                 MONEYBIRD_ADMINISTRATION_ID "
                f"({env_administration}) overrides the stored "
                f"administration ({connection.administration_id})."
            )
    else:
        _out(
            "Active identity:        none. No personal token and no OAuth "
            f"connection; run '{PROG} login'."
        )
    return 0


def command_logout(args: argparse.Namespace) -> int:
    removed = oauth.delete_connection(args.profile)
    if removed:
        _out(f"Local OAuth credentials for profile {args.profile!r} were deleted.")
    else:
        _out(f"No stored OAuth credentials for profile {args.profile!r}.")

    # Deleting a file is not revocation, and saying "logged out" without this
    # would leave the user believing access had been withdrawn.
    if not oauth.REVOCATION_SUPPORTED:
        _out(
            "\nThis removed local credentials only. Moneybird publishes no "
            "token revocation endpoint, so the authorization itself is still "
            "valid until you withdraw it in Moneybird:\n"
            f"  {oauth.APPLICATIONS_URL}"
        )
    return 0


def command_scopes(_: argparse.Namespace) -> int:
    """Print the scope rationale, so a user can review before authorizing."""
    _out("Moneybird OAuth scopes requested by this server\n")
    _out(
        "Moneybird assigns scopes per endpoint, and the grouping is not the\n"
        "intuitive one: reports are scoped individually rather than sharing a\n"
        "single scope, and financial accounts are settings while financial\n"
        "mutations are bank. Each line below names what Moneybird's own endpoint\n"
        "reference requires.\n"
    )
    _out(
        "Scopes are per resource family and have no read-only variant. They do\n"
        "not control whether this server may write: that is\n"
        "MONEYBIRD_CAPABILITY_MODE and the prepare/approve/execute flow,\n"
        "enforced locally.\n"
    )
    for entry in CAPABILITY_SCOPES:
        needs = " + ".join(entry.scopes)
        _out(f"{needs:<28} {entry.area}")
        _out(f"{'':<28} {entry.reason}")
        _out(f"{'':<28} e.g. {', '.join(entry.examples[:4])}")
        _out("")

    _out("Reachable without a scope of their own:")
    for area, scopes, _endpoints in INCIDENTAL_ACCESS:
        allowed = f"any of: {', '.join(scopes)}" if scopes else "no scope required"
        _out(f"  {area} ({allowed})")

    _out("\nNamed profiles (--scopes NAME, or " + SCOPES_ENV + "):")
    for name, scopes in sorted(SCOPE_PROFILES.items()):
        _out(f"  {name:<12} {format_scopes(scopes)}")
        lost = unavailable_areas(scopes)
        _out(f"  {'':<12} unavailable: {', '.join(lost) if lost else 'nothing'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Connect this Moneybird MCP installation to a Moneybird account "
            "through OAuth, inspect the connection, or remove it."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument(
            "--env-file",
            metavar="PATH",
            default=None,
            help=(
                "Explicitly selected configuration file holding "
                f"{oauth.CLIENT_ID_ENV} / {oauth.CLIENT_SECRET_ENV}. Values "
                "already present in the parent environment win. No .env file "
                "is discovered automatically."
            ),
        )
        subparser.add_argument(
            "--profile",
            default=DEFAULT_PROFILE,
            help="Store or read the connection under this profile name.",
        )

    login = subparsers.add_parser(
        "login", help="Authorize this installation against Moneybird."
    )
    add_common(login)
    login.add_argument(
        "--redirect-uri",
        default=oauth.OOB_REDIRECT_URI,
        help=(
            "Registered redirect URI. Defaults to the out-of-band URI, which "
            "makes Moneybird display the code instead of redirecting."
        ),
    )
    login.add_argument(
        "--scopes",
        default=None,
        help=(
            "Scope profile name or explicit space-separated scopes. Defaults "
            f"to {SCOPES_ENV} or the full set. See '{PROG} scopes'."
        ),
    )
    login.add_argument(
        "--administration",
        default=None,
        help="Administration id to use, instead of being asked.",
    )
    login.add_argument(
        "--no-browser",
        action="store_true",
        help="Only print the authorization URL; never try to open a browser.",
    )
    login.set_defaults(handler=command_login)

    status = subparsers.add_parser(
        "status", help="Show which Moneybird identity is configured."
    )
    add_common(status)
    status.set_defaults(handler=command_status)

    logout = subparsers.add_parser(
        "logout", help="Delete this installation's local OAuth credentials."
    )
    add_common(logout)
    logout.set_defaults(handler=command_logout)

    scopes = subparsers.add_parser(
        "scopes", help="Explain which Moneybird scopes this server requests and why."
    )
    scopes.set_defaults(handler=command_scopes)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    env_file = getattr(args, "env_file", None)
    if env_file is not None:
        try:
            load_env_file(env_file)
        except (MoneybirdError, OSError, UnicodeError) as exc:
            parser.error(str(exc))
    _default_data_dir()

    try:
        return int(args.handler(args))
    except MoneybirdError as exc:
        _err(f"\n{exc}")
        return 1
    except KeyboardInterrupt:
        _err("\nAborted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
