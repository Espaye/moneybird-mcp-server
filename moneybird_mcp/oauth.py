"""OAuth 2.0 authorization-code flow against Moneybird.

Verified against https://developer.moneybird.com/authentication (2026-08-08):

- authorize: ``https://moneybird.com/oauth/authorize``
- token:     ``https://moneybird.com/oauth/token``
- ``urn:ietf:wg:oauth:2.0:oob`` suppresses the redirect and makes Moneybird
  display the authorization code in the browser instead.
- Access tokens do not currently expire — no ``expires_in`` comes back — but the
  documentation asks integrations to store the refresh token and be ready for
  that to change. :func:`get_access_token` therefore refreshes on expiry
  metadata when it appears, and does nothing extra while it does not.
- Moneybird documents **no revocation endpoint**. See :data:`REVOCATION_SUPPORTED`.

This module is the protocol layer and holds no presentation logic: URL
construction, the two token grants, and the refresh-on-read session helper. The
CLI lives in :mod:`moneybird_mcp.auth_cli`, persistence in
:mod:`moneybird_mcp.oauth_store`, and the scope rationale in
:mod:`moneybird_mcp.oauth_scopes`. A hosted callback flow reuses everything here
and swaps only the first two.

Nothing in this module logs, and no exception it raises carries a client secret,
an authorization code, an access token, or a refresh token.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_ERROR_DETAIL_CHARS,
    MoneybirdError,
)
from .oauth_scopes import (
    DEFAULT_SCOPE_PROFILE,
    SCOPES_ENV,
    format_scopes,
    parse_scopes,
    scopes_for_profile,
)
from .oauth_store import (
    DEFAULT_PROFILE,
    OAuthConnection,
    get_token_store,
)

OAUTH_AUTHORIZE_URL = "https://moneybird.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://moneybird.com/oauth/token"

# Where a user registers an application and, because there is no revocation API,
# where they withdraw an authorization again.
APPLICATIONS_URL = "https://moneybird.com/user/applications"
REGISTER_APPLICATION_URL = "https://moneybird.com/user/applications/new"

# Out-of-band: Moneybird shows the authorization code in the browser instead of
# redirecting, so no reachable callback endpoint is needed. This is the local /
# development mechanism; a hosted product registers an HTTPS callback instead.
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

CLIENT_ID_ENV = "MONEYBIRD_OAUTH_CLIENT_ID"
CLIENT_SECRET_ENV = "MONEYBIRD_OAUTH_CLIENT_SECRET"

# Moneybird documents no token revocation endpoint (checked 2026-08-08), so
# `auth logout` can only delete local credentials. Access is withdrawn by the
# user at APPLICATIONS_URL. Kept as a named constant rather than a comment
# because the CLI has to tell the user which of the two actually happened.
REVOCATION_SUPPORTED = False

# The scope string a default login requests. Kept as a module constant because
# it is part of this module's published surface.
DEFAULT_OAUTH_SCOPES = format_scopes(scopes_for_profile(DEFAULT_SCOPE_PROFILE))

# Refresh this many seconds before the reported expiry to absorb clock skew.
EXPIRY_MARGIN_SECONDS = 60

# Bound on how much of an error body is read before parsing. A token endpoint
# error is a short JSON object; anything larger is not something to buffer.
_MAX_ERROR_BODY_BYTES = 8192

__all__ = [
    "APPLICATIONS_URL",
    "DEFAULT_OAUTH_SCOPES",
    "DEFAULT_PROFILE",
    "EXPIRY_MARGIN_SECONDS",
    "OAUTH_AUTHORIZE_URL",
    "OAUTH_TOKEN_URL",
    "OOB_REDIRECT_URI",
    "REGISTER_APPLICATION_URL",
    "REVOCATION_SUPPORTED",
    "build_authorize_url",
    "configured_scopes",
    "credential_location",
    "delete_connection",
    "exchange_authorization_code",
    "generate_state",
    "get_access_token",
    "get_connection",
    "load_connection",
    "load_tokens",
    "oauth_client_config",
    "oauth_tokens_path",
    "parse_authorization_callback",
    "refresh_access_token",
    "save_connection",
    "store_tokens",
]


def oauth_client_config() -> tuple[str, str]:
    """The registered application's (client_id, client_secret) from the environment.

    Both come from the parent process environment, optionally populated by an
    operator-selected ``--env-file``. No ``.env`` is ever discovered
    automatically; see :func:`moneybird_mcp.config.load_env_file`.
    """
    client_id = os.environ.get(CLIENT_ID_ENV, "").strip()
    client_secret = os.environ.get(CLIENT_SECRET_ENV, "").strip()
    if not client_id or not client_secret:
        missing = " and ".join(
            name
            for name, value in ((CLIENT_ID_ENV, client_id), (CLIENT_SECRET_ENV, client_secret))
            if not value
        )
        raise MoneybirdError(
            f"{missing} is not set. Register an application at "
            f"{REGISTER_APPLICATION_URL} with redirect URI "
            f"{OOB_REDIRECT_URI}, then supply both values in the parent "
            "environment or through the explicit --env-file option."
        )
    return client_id, client_secret


def configured_scopes() -> tuple[str, ...]:
    """Scopes to request, from :data:`~moneybird_mcp.oauth_scopes.SCOPES_ENV`."""
    return parse_scopes(os.environ.get(SCOPES_ENV, ""))


def build_authorize_url(
    *,
    redirect_uri: str = OOB_REDIRECT_URI,
    scope: str | None = None,
    state: str = "",
) -> str:
    """The URL the user visits in a browser to grant access.

    ``redirect_uri`` must exactly match one registered on the application;
    Moneybird rejects any mismatch. It is a caller-supplied value rather than
    user input: the OOB default serves the CLI, and a hosted flow passes its own
    HTTPS callback.
    """
    client_id, _ = oauth_client_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope if scope is not None else format_scopes(configured_scopes()),
    }
    if state:
        params["state"] = state
    return f"{OAUTH_AUTHORIZE_URL}?{urllib.parse.urlencode(params)}"


def generate_state() -> str:
    """A cryptographically random CSRF ``state`` value for :func:`build_authorize_url`.

    Hosted (redirect-URI) flows must generate this per login attempt, keep it in the
    user's session, and pass it to :func:`parse_authorization_callback`.
    """
    return secrets.token_urlsafe(32)


def parse_authorization_callback(
    callback_url: str,
    *,
    expected_state: str = "",
) -> str:
    """Extract the authorization code from an OAuth redirect callback URL.

    Raises :class:`MoneybirdError` when the provider reported an error (e.g. the
    user denied consent), when ``expected_state`` is given and does not match the
    callback's ``state`` (CSRF), or when no code is present. Hosted flows must
    always pass ``expected_state``; it is optional only for local development.
    """
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(callback_url).query)
    error = (query.get("error") or [""])[0]
    if error:
        description = (query.get("error_description") or [""])[0]
        raise MoneybirdError(
            f"Moneybird authorization failed: {error} {description}".strip()
        )
    if expected_state:
        state = (query.get("state") or [""])[0]
        if not secrets.compare_digest(state, expected_state):
            raise MoneybirdError(
                "OAuth state mismatch on the callback: possible CSRF; "
                "not exchanging the authorization code."
            )
    code = (query.get("code") or [""])[0].strip()
    if not code:
        raise MoneybirdError("The callback URL contains no authorization code.")
    return code


# --- Token endpoint ----------------------------------------------------------
#
# Neither grant is retried. An authorization code is single-use, so repeating a
# request that timed out after Moneybird already consumed it turns a recoverable
# situation into `invalid_grant`; and a refresh may rotate the refresh token, so
# a blind repeat can invalidate the copy still on disk. The project's retry
# convention applies to idempotent reads, which these are not.


def _oauth_error_detail(body: str) -> str:
    """The RFC 6749 error fields from a token-endpoint error body, if any.

    Only ``error`` and ``error_description`` are extracted, and only when they
    are strings. Rendering the body wholesale is not acceptable here: the token
    endpoint is the one endpoint whose responses contain credentials, and a
    partial success (tokens plus a warning) must not end up in a message, a
    traceback, or the audit log.
    """
    try:
        payload = json.loads(body)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    code = payload.get("error")
    description = payload.get("error_description")
    parts = [
        part.strip()
        for part in (code, description)
        if isinstance(part, str) and part.strip()
    ]
    return ": ".join(parts)[:MAX_ERROR_DETAIL_CHARS]


# Guidance per documented OAuth error code. HTTP status alone does not
# distinguish "your client secret is wrong" from "that code was already used",
# and those need opposite responses from the user.
_ERROR_GUIDANCE = {
    "invalid_client": (
        f"{CLIENT_ID_ENV} / {CLIENT_SECRET_ENV} were rejected. Check them "
        f"against the application at {APPLICATIONS_URL}."
    ),
    "invalid_grant": (
        "The authorization code was rejected: codes are single-use and expire "
        "quickly, and the redirect URI must match the one used to request them. "
        "Start the login again and paste the new code promptly."
    ),
    "invalid_request": (
        "Moneybird rejected the request's parameters. Confirm the application's "
        "registered redirect URI matches the one being used."
    ),
    "invalid_scope": (
        f"One of the requested scopes was rejected. Check {SCOPES_ENV}."
    ),
    "unauthorized_client": (
        "This application is not allowed to use that grant type. Check the "
        f"application's configuration at {APPLICATIONS_URL}."
    ),
    "access_denied": "The authorization was refused.",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to follow a redirect away from the token endpoint.

    urllib would turn a 302 into a GET of the redirect target and hand the body
    back as if the token endpoint had answered — so whatever that target served
    would be parsed as a token response and persisted as the access token. Only
    Moneybird (or something that has already broken TLS) could trigger it, but
    the endpoint that mints credentials is the wrong place to accept a
    redirect. ``client.py`` refuses redirects on its credential-bearing
    requests for the same reason.

    Returning None makes urllib raise the original HTTPError instead.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D102
        return None


# Module-level so the handler chain is built once. It carries no credentials and
# no cookie jar: every request supplies its own form body.
_TOKEN_OPENER = urllib.request.build_opener(_NoRedirect)


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    """POST ``form`` to the token endpoint and return the parsed JSON response.

    The URL is this module's own HTTPS constant, never anything derived from
    configuration or user input, so TLS validation applies with the default
    certificate store and no endpoint substitution is possible.
    """
    request = urllib.request.Request(
        url=OAUTH_TOKEN_URL,
        method="POST",
        data=urllib.parse.urlencode(form).encode("utf-8"),
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    try:
        # The request URL is the module-owned HTTPS Moneybird token endpoint above.
        with _TOKEN_OPENER.open(  # nosec B310
            request,
            timeout=DEFAULT_TIMEOUT_SECONDS,
        ) as response:
            payload = response.read(_MAX_ERROR_BODY_BYTES * 8).decode(
                "utf-8", errors="replace"
            )
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(_MAX_ERROR_BODY_BYTES).decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - a failed error read must not mask the error
            body = ""
        detail = _oauth_error_detail(body)
        code = detail.split(":", 1)[0].strip() if detail else ""
        guidance = _ERROR_GUIDANCE.get(code, "")
        if not detail:
            detail = (
                "Moneybird sent no machine-readable reason; the response was "
                "withheld because it may contain credentials."
            )
        if not guidance and 500 <= exc.code < 600:
            guidance = "This is an error on Moneybird's side; try again shortly."
        message = f"Moneybird token request failed with HTTP {exc.code}. {detail}"
        raise MoneybirdError(f"{message} {guidance}".strip()) from None
    except TimeoutError:
        raise MoneybirdError(
            f"The Moneybird token endpoint did not respond within "
            f"{DEFAULT_TIMEOUT_SECONDS} seconds. Nothing was retried "
            "automatically, because an authorization code is single-use."
        ) from None
    except urllib.error.URLError as exc:
        raise MoneybirdError(
            "Could not reach the Moneybird token endpoint "
            f"({type(exc.reason).__name__ if exc.reason is not None else 'network error'}). "
            "Check network connectivity and TLS interception, then try again."
        ) from None
    except OSError:
        raise MoneybirdError(
            "Could not reach the Moneybird token endpoint."
        ) from None

    try:
        tokens = json.loads(payload)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise MoneybirdError(
            "Moneybird returned an invalid token response (not JSON)."
        ) from None
    if (
        not isinstance(tokens, dict)
        or not isinstance(tokens.get("access_token"), str)
        or not tokens["access_token"].strip()
    ):
        raise MoneybirdError(
            "Moneybird returned a token response without a valid access token."
        )
    return tokens


def exchange_authorization_code(
    code: str,
    *,
    redirect_uri: str = OOB_REDIRECT_URI,
) -> dict[str, Any]:
    """Trade a one-time authorization code for access/refresh tokens.

    ``redirect_uri`` must be the same value used in :func:`build_authorize_url`
    (and registered on the application) — Moneybird rejects mismatches.
    """
    client_id, client_secret = oauth_client_config()
    cleaned = (code or "").strip()
    if not cleaned:
        raise MoneybirdError("No authorization code was supplied.")
    # A pasted code sometimes arrives with surrounding markup or a whole URL.
    # Reject that here rather than sending it: the resulting `invalid_grant`
    # looks identical to an expired code and sends the user down the wrong path.
    if len(cleaned.split()) > 1 or "://" in cleaned:
        raise MoneybirdError(
            "That does not look like a Moneybird authorization code. Paste only "
            "the code shown in the browser, with no surrounding text or URL."
        )
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": cleaned,
            "redirect_uri": redirect_uri,
        }
    )


def refresh_access_token(refresh_token: str) -> dict[str, Any]:
    """Obtain a fresh access token (and possibly a rotated refresh token)."""
    client_id, client_secret = oauth_client_config()
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
    )


# --- Connections -------------------------------------------------------------

# One lock per profile, so a refresh for one connection cannot block another.
# The lock is held across the network call on purpose: two concurrent refreshes
# of the same connection would race to persist, and if Moneybird ever rotates
# refresh tokens the loser would overwrite the live one with a dead value.
_profile_locks: defaultdict[str, threading.RLock] = defaultdict(threading.RLock)
_locks_guard = threading.Lock()


def _profile_lock(profile: str) -> threading.RLock:
    with _locks_guard:
        return _profile_locks[profile]


def credential_location() -> str:
    """Where the active store keeps credentials, as text.

    Text, not a path: a hosted store's location is a database or secret-manager
    reference, and forcing that through :class:`~pathlib.Path` corrupts it.
    Anything that only displays the location must use this.
    """
    return get_token_store().location()


def oauth_tokens_path() -> Path:
    """The local credential file's path.

    Only meaningful while the active store is the local
    :class:`~moneybird_mcp.oauth_store.FileTokenStore`; use
    :func:`credential_location` for display.
    """
    return Path(credential_location())


def load_connection(profile: str = DEFAULT_PROFILE) -> OAuthConnection | None:
    """The stored connection for ``profile``, without contacting Moneybird."""
    return get_token_store().load(profile)


def save_connection(
    connection: OAuthConnection, *, profile: str = DEFAULT_PROFILE
) -> None:
    get_token_store().save(connection, profile=profile)


def delete_connection(profile: str = DEFAULT_PROFILE) -> bool:
    """Remove local credentials for ``profile``. Returns False if there were none.

    This deletes only local state. Moneybird publishes no revocation endpoint
    (:data:`REVOCATION_SUPPORTED`), so the grant itself stays valid until the
    user withdraws it at :data:`APPLICATIONS_URL`.
    """
    return get_token_store().delete(profile)


def get_connection(profile: str = DEFAULT_PROFILE) -> OAuthConnection | None:
    """The connection for ``profile``, refreshed and re-persisted if expired.

    Returns ``None`` when no OAuth login has been performed for the profile, so
    callers can fall through to other credential sources. A refresh failure
    raises and leaves the stored connection untouched — a temporarily
    unreachable Moneybird must not cost the user their refresh token.
    """
    with _profile_lock(profile):
        connection = load_connection(profile)
        if connection is None:
            return None
        if not connection.is_expired(margin_seconds=EXPIRY_MARGIN_SECONDS):
            return connection
        if not connection.refresh_token:
            raise MoneybirdError(
                f"The stored OAuth access token for profile {profile!r} has "
                "expired and no refresh token is stored. Run "
                "'moneybird-mcp auth login' again."
            )
        try:
            payload = refresh_access_token(connection.refresh_token)
        except MoneybirdError as exc:
            # Re-raised, never swallowed, and the stored connection is left as
            # it was: a network blip is not a reason to discard credentials.
            raise MoneybirdError(
                f"Could not refresh the Moneybird access token for profile "
                f"{profile!r}: {exc} The stored credentials were left "
                "unchanged. If Moneybird reports the grant as invalid or "
                "revoked, run 'moneybird-mcp auth login' to reconnect."
            ) from None
        refreshed = connection.merged_with_refresh(payload)
        save_connection(refreshed, profile=profile)
        return refreshed


def get_access_token(profile: str = DEFAULT_PROFILE) -> str | None:
    """A valid access token for ``profile``, refreshing (and persisting) if expired."""
    connection = get_connection(profile)
    return connection.access_token if connection else None


def get_administration_id(profile: str = DEFAULT_PROFILE) -> str | None:
    """The administration selected during login, if one was stored.

    Read without refreshing: this answers a configuration question and must not
    trigger network I/O on a path that only needs local state.
    """
    connection = load_connection(profile)
    return connection.administration_id if connection else None


# --- Backward-compatible dict API --------------------------------------------
#
# The gateway demo and existing callers pass raw Moneybird token responses
# around. Keep that working on top of the typed store.


def store_tokens(
    tokens: dict[str, Any], *, profile: str = DEFAULT_PROFILE
) -> OAuthConnection:
    """Persist a raw token response under ``profile`` and return what was stored.

    An administration already selected for this profile is preserved: it is
    local state, not part of the grant, and a re-login should not silently move
    the user back to auto-selection.
    """
    existing = load_connection(profile)
    connection = OAuthConnection.from_token_response(
        tokens,
        administration_id=existing.administration_id if existing else None,
    )
    save_connection(connection, profile=profile)
    return connection


def load_tokens(profile: str = DEFAULT_PROFILE) -> dict[str, Any] | None:
    """The stored token record for ``profile`` as a plain dict, or ``None``."""
    connection = load_connection(profile)
    return connection.to_record() if connection else None
