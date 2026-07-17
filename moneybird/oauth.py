"""OAuth 2.0 authorization-code flow against Moneybird.

Moneybird's OAuth endpoints (see https://developer.moneybird.com/authentication):

- authorize: ``https://moneybird.com/oauth/authorize``
- token:     ``https://moneybird.com/oauth/token``

The app is registered at https://moneybird.com/user/applications/new; its
``client_id`` / ``client_secret`` are read from the environment
(``MONEYBIRD_OAUTH_CLIENT_ID`` / ``MONEYBIRD_OAUTH_CLIENT_SECRET``, normally via
``.env``). The registered redirect URI must match ``redirect_uri`` exactly;
``urn:ietf:wg:oauth:2.0:oob`` makes Moneybird display the code in the browser
instead of redirecting, which is how ``scripts/oauth_login.py`` works.

Obtained tokens are persisted per profile in ``moneybird_oauth_tokens.json``
inside :func:`moneybird.config.data_dir`, and :func:`get_access_token`
transparently refreshes an expired access token (persisting the rotated refresh
token). Moneybird access tokens historically do not expire (no ``expires_in``
in the response); the refresh path exists for when they do.
"""
from __future__ import annotations

import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import DEFAULT_TIMEOUT_SECONDS, MoneybirdError, data_dir

OAUTH_AUTHORIZE_URL = "https://moneybird.com/oauth/authorize"
OAUTH_TOKEN_URL = "https://moneybird.com/oauth/token"

# Out-of-band: Moneybird shows the authorization code in the browser instead of
# redirecting, so no reachable callback endpoint is needed (local development).
OOB_REDIRECT_URI = "urn:ietf:wg:oauth:2.0:oob"

# Everything the tool surface touches. The Moneybird default (sales_invoices
# only) is far too narrow: documents covers purchase invoices/receipts, bank
# covers financial accounts/mutations, settings covers ledger accounts, tax
# rates, workflows and the other reference data, and time_entries backs
# list_time_entries (a token without it gets 401 on that endpoint).
DEFAULT_OAUTH_SCOPES = "sales_invoices documents estimates bank time_entries settings"

# Refresh this many seconds before the reported expiry to absorb clock skew.
EXPIRY_MARGIN_SECONDS = 60

DEFAULT_PROFILE = "default"


def oauth_client_config() -> tuple[str, str]:
    """The registered application's (client_id, client_secret) from the environment."""
    client_id = os.environ.get("MONEYBIRD_OAUTH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("MONEYBIRD_OAUTH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        raise MoneybirdError(
            "MONEYBIRD_OAUTH_CLIENT_ID / MONEYBIRD_OAUTH_CLIENT_SECRET are not set. "
            "Register an application at https://moneybird.com/user/applications/new "
            "and put both values in .env."
        )
    return client_id, client_secret


def build_authorize_url(
    *,
    redirect_uri: str = OOB_REDIRECT_URI,
    scope: str = DEFAULT_OAUTH_SCOPES,
    state: str = "",
) -> str:
    """The URL the user visits in a browser to grant access."""
    client_id, _ = oauth_client_config()
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": scope,
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


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    """POST ``form`` to the token endpoint and return the parsed JSON response."""
    request = urllib.request.Request(
        url=OAUTH_TOKEN_URL,
        method="POST",
        data=urllib.parse.urlencode(form).encode("utf-8"),
    )
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=DEFAULT_TIMEOUT_SECONDS) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise MoneybirdError(
            f"Moneybird token endpoint returned HTTP {exc.code}: {body[:500]}"
        ) from exc
    except urllib.error.URLError as exc:
        raise MoneybirdError(f"Could not reach the Moneybird token endpoint: {exc}") from exc
    tokens = json.loads(payload)
    if "access_token" not in tokens:
        raise MoneybirdError(f"Token response contains no access_token: {tokens}")
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
    return _token_request(
        {
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "authorization_code",
            "code": code.strip(),
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


# --- Token persistence -------------------------------------------------------


def oauth_tokens_path() -> Path:
    return data_dir() / "moneybird_oauth_tokens.json"


def _load_store() -> dict[str, dict[str, Any]]:
    path = oauth_tokens_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _save_store(store: dict[str, dict[str, Any]]) -> None:
    oauth_tokens_path().write_text(
        json.dumps(store, indent=2, sort_keys=True), encoding="utf-8"
    )


def store_tokens(tokens: dict[str, Any], *, profile: str = DEFAULT_PROFILE) -> None:
    """Persist a token response under ``profile``, stamping when it was obtained."""
    record = dict(tokens)
    record.setdefault("obtained_at", int(time.time()))
    store = _load_store()
    store[profile] = record
    _save_store(store)


def load_tokens(profile: str = DEFAULT_PROFILE) -> dict[str, Any] | None:
    return _load_store().get(profile)


def _is_expired(record: dict[str, Any]) -> bool:
    expires_in = record.get("expires_in")
    if not expires_in:  # Moneybird tokens without expires_in never expire.
        return False
    obtained_at = record.get("obtained_at") or record.get("created_at") or 0
    return time.time() >= float(obtained_at) + float(expires_in) - EXPIRY_MARGIN_SECONDS


def get_access_token(profile: str = DEFAULT_PROFILE) -> str | None:
    """A valid access token for ``profile``, refreshing (and persisting) if expired.

    Returns ``None`` when no OAuth login has been performed for the profile, so
    callers can fall through to other credential sources.
    """
    record = load_tokens(profile)
    if not record:
        return None
    if _is_expired(record):
        refresh_token = record.get("refresh_token")
        if not refresh_token:
            raise MoneybirdError(
                f"The stored OAuth access token for profile {profile!r} has expired and "
                "no refresh token is stored. Run scripts/oauth_login.py again."
            )
        record = refresh_access_token(refresh_token)
        store_tokens(record, profile=profile)
    return record["access_token"]
