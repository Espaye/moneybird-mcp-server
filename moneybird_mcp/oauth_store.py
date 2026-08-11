"""The OAuth connection model and where it is persisted.

One *connection* is one Moneybird identity this installation may act as: the
tokens obtained for it, the scopes Moneybird granted, and the administration
selected for it. :class:`TokenStore` is the only interface the rest of the
package uses to reach one, so another integration can provide a different store
without touching :mod:`moneybird_mcp.client` or the tool surface.

Locally the store is a single JSON file in :func:`moneybird_mcp.config.data_dir`,
keyed by profile name. That on-disk shape predates this module and is preserved
exactly, so an existing ``moneybird_oauth_tokens.json`` keeps working: unknown
keys in a record are carried through untouched rather than dropped.

Nothing here logs. :class:`OAuthConnection` redacts its own repr because a token
reaches a traceback, a debugger, or a ``%r`` in someone's print statement far
more easily than it reaches a log call anyone reviewed.
"""
from __future__ import annotations

import json
import os
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .config import MoneybirdError, data_dir, harden_private_file

DEFAULT_PROFILE = "default"

# Selects which stored connection this process acts as. One value, read through
# `active_profile()` everywhere, because a profile the CLI can write but the
# server cannot read is worse than no profile support at all: `auth status`
# would report a connection as active that nothing ever loads.
PROFILE_ENV = "MONEYBIRD_OAUTH_PROFILE"

STORE_FILENAME = "moneybird_oauth_tokens.json"

# Keys this module owns. Anything else Moneybird sends is preserved verbatim in
# `extra` so a future token-response field is not silently discarded on refresh.
_KNOWN_KEYS = frozenset(
    {
        "access_token",
        "refresh_token",
        "token_type",
        "scope",
        "expires_in",
        "created_at",
        "obtained_at",
        "administration_id",
    }
)

_REDACTED = "<redacted>"


def active_profile() -> str:
    """The profile this process uses when a caller names none.

    Read at call time, not import time, so a test or a long-running process can
    redirect it the same way it can redirect the data directory.
    """
    return os.environ.get(PROFILE_ENV, "").strip() or DEFAULT_PROFILE


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class OAuthConnection:
    """Tokens plus the metadata needed to use and renew them.

    ``obtained_at`` is this client's own receipt timestamp, not Moneybird's:
    Moneybird's ``created_at`` (when present) records when the *grant* was
    created, which is not when this particular access token started its life.
    Expiry is judged against the local stamp for that reason.
    """

    access_token: str
    refresh_token: str = ""
    token_type: str = "bearer"
    scope: str = ""
    expires_in: int | None = None
    obtained_at: int = 0
    created_at: int | None = None
    administration_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.access_token:
            raise MoneybirdError("An OAuth connection requires an access token.")

    # --- Redaction -----------------------------------------------------------
    #
    # dataclass' generated __repr__ prints every field, so it has to be replaced
    # rather than merely supplemented.

    def __repr__(self) -> str:
        refresh = _REDACTED if self.refresh_token else ""
        return (
            f"OAuthConnection(access_token={_REDACTED!r}, "
            f"refresh_token={refresh!r}, "
            f"token_type={self.token_type!r}, scope={self.scope!r}, "
            f"expires_in={self.expires_in!r}, obtained_at={self.obtained_at!r}, "
            f"administration_id={self.administration_id!r})"
        )

    __str__ = __repr__

    def describe(self) -> dict[str, Any]:
        """A summary safe to print, return from a tool, or write to a log.

        Deliberately reports token *presence* and length only. "Is a refresh
        token stored?" is the question a user debugging a connection actually
        has, and it can be answered without revealing any part of the value —
        no prefix, no suffix, no fingerprint.
        """
        return {
            "has_access_token": bool(self.access_token),
            "has_refresh_token": bool(self.refresh_token),
            "access_token_length": len(self.access_token),
            "token_type": self.token_type or "bearer",
            "scope": self.scope,
            "expires_in": self.expires_in,
            "obtained_at": self.obtained_at,
            "expires_at": self.expires_at,
            "administration_id": self.administration_id,
        }

    @property
    def expires_at(self) -> int | None:
        """Absolute expiry, or ``None`` for a token that does not expire.

        Moneybird access tokens currently carry no ``expires_in``; the
        documentation asks integrations to be ready for that to change.
        """
        if not self.expires_in:
            return None
        return int(self.obtained_at) + int(self.expires_in)

    def is_expired(self, *, margin_seconds: int = 0, now: float | None = None) -> bool:
        expires_at = self.expires_at
        if expires_at is None:
            return False
        current = time.time() if now is None else now
        return current >= expires_at - margin_seconds

    # --- Serialization -------------------------------------------------------

    @classmethod
    def from_token_response(
        cls,
        payload: dict[str, Any],
        *,
        obtained_at: int | None = None,
        administration_id: str | None = None,
    ) -> OAuthConnection:
        access_token = _text(payload.get("access_token"))
        if not access_token:
            raise MoneybirdError(
                "Moneybird returned a token response without a valid access token."
            )
        return cls(
            access_token=access_token,
            refresh_token=_text(payload.get("refresh_token")),
            token_type=_text(payload.get("token_type")) or "bearer",
            scope=_text(payload.get("scope")),
            expires_in=_optional_int(payload.get("expires_in")),
            obtained_at=(
                int(obtained_at)
                if obtained_at is not None
                else _optional_int(payload.get("obtained_at")) or int(time.time())
            ),
            created_at=_optional_int(payload.get("created_at")),
            administration_id=_text(payload.get("administration_id")) or administration_id,
            extra={
                key: value
                for key, value in payload.items()
                if key not in _KNOWN_KEYS
            },
        )

    def to_record(self) -> dict[str, Any]:
        record: dict[str, Any] = dict(self.extra)
        record["access_token"] = self.access_token
        record["token_type"] = self.token_type
        record["obtained_at"] = self.obtained_at
        if self.refresh_token:
            record["refresh_token"] = self.refresh_token
        if self.scope:
            record["scope"] = self.scope
        if self.expires_in is not None:
            record["expires_in"] = self.expires_in
        if self.created_at is not None:
            record["created_at"] = self.created_at
        if self.administration_id:
            record["administration_id"] = self.administration_id
        return record

    def merged_with_refresh(self, payload: dict[str, Any]) -> OAuthConnection:
        """This connection updated by a refresh-token grant response.

        Moneybird may answer a refresh with only a new access token. Replacing
        the whole record with that response would drop the refresh token and
        the granted scopes, turning the next expiry into a forced re-login. So
        an absent field means "unchanged", never "cleared".
        """
        refreshed = OAuthConnection.from_token_response(payload)
        return OAuthConnection(
            access_token=refreshed.access_token,
            refresh_token=refreshed.refresh_token or self.refresh_token,
            token_type=refreshed.token_type or self.token_type,
            scope=refreshed.scope or self.scope,
            expires_in=(
                refreshed.expires_in
                if "expires_in" in payload
                else self.expires_in
            ),
            obtained_at=refreshed.obtained_at,
            created_at=refreshed.created_at if refreshed.created_at else self.created_at,
            # The selected administration is local state, not part of the grant.
            administration_id=self.administration_id,
            extra={**self.extra, **refreshed.extra},
        )

    def with_administration(self, administration_id: str | None) -> OAuthConnection:
        return OAuthConnection(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            token_type=self.token_type,
            scope=self.scope,
            expires_in=self.expires_in,
            obtained_at=self.obtained_at,
            created_at=self.created_at,
            administration_id=(administration_id or "").strip() or None,
            extra=dict(self.extra),
        )


class TokenStore(Protocol):
    """Where OAuth connections live.

    Another integration can implement this over its own protected storage and
    register it with :func:`set_token_store`. Every method takes the profile
    explicitly; there is no ambient "current connection" assumption.
    """

    def load(self, profile: str = DEFAULT_PROFILE) -> OAuthConnection | None: ...

    def save(
        self, connection: OAuthConnection, *, profile: str = DEFAULT_PROFILE
    ) -> None: ...

    def delete(self, profile: str = DEFAULT_PROFILE) -> bool: ...

    def profiles(self) -> tuple[str, ...]: ...

    def location(self) -> str:
        """Human-readable description of where credentials are kept."""
        ...


class FileTokenStore:
    """The local implementation: one owner-only JSON file in the data dir.

    Writes go through a temporary file in the same directory and an atomic
    replace, so an interrupted save cannot truncate a working connection. The
    lock is process-local; concurrent *processes* are not coordinated, which is
    acceptable for a single user's machine. Other deployment boundaries can
    replace it with coordinated storage.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._explicit_path = path
        self._lock = threading.RLock()

    @property
    def path(self) -> Path:
        # Resolved per call, not cached: data_dir() reads MONEYBIRD_MCP_DATA_DIR
        # at call time so tests and long-running processes can redirect state.
        return self._explicit_path or (data_dir() / STORE_FILENAME)

    def location(self) -> str:
        return str(self.path)

    def _read_all(self) -> dict[str, dict[str, Any]]:
        path = self.path
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise MoneybirdError(
                f"The OAuth credential file at {path} could not be read "
                f"({type(exc).__name__}). Move it aside and log in again."
            ) from None
        if not isinstance(raw, dict):
            raise MoneybirdError(
                f"The OAuth credential file at {path} is not a credential store. "
                "Move it aside and log in again."
            )
        return {
            key: value for key, value in raw.items() if isinstance(value, dict)
        }

    def _write_all(self, store: dict[str, dict[str, Any]]) -> None:
        path = self.path
        temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_text(
                json.dumps(store, indent=2, sort_keys=True), encoding="utf-8"
            )
            harden_private_file(temporary)
            os.replace(temporary, path)
            harden_private_file(path)
        except OSError as exc:
            raise MoneybirdError(
                f"Could not write the OAuth credential file at {path}: "
                f"{exc.strerror or type(exc).__name__}. Check the directory "
                "exists and is writable, or set MONEYBIRD_MCP_DATA_DIR to a "
                "location you own."
            ) from None
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self, profile: str = DEFAULT_PROFILE) -> OAuthConnection | None:
        with self._lock:
            record = self._read_all().get(profile)
        if not record:
            return None
        try:
            return OAuthConnection.from_token_response(record)
        except MoneybirdError:
            # A record without a usable access token is not a connection. Say so
            # by returning None so callers fall through to their other sources,
            # rather than failing every request on a stale artifact.
            return None

    def save(
        self, connection: OAuthConnection, *, profile: str = DEFAULT_PROFILE
    ) -> None:
        with self._lock:
            store = self._read_all()
            store[profile] = connection.to_record()
            self._write_all(store)

    def delete(self, profile: str = DEFAULT_PROFILE) -> bool:
        with self._lock:
            store = self._read_all()
            if profile not in store:
                return False
            del store[profile]
            if store:
                self._write_all(store)
            else:
                # Nothing left to protect: remove the file instead of leaving an
                # empty one that reads like a configured-but-broken connection.
                try:
                    self.path.unlink(missing_ok=True)
                except OSError as exc:
                    raise MoneybirdError(
                        f"Could not remove the OAuth credential file at "
                        f"{self.path}: {exc.strerror or type(exc).__name__}."
                    ) from None
            return True

    def profiles(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._read_all()))


_store: TokenStore = FileTokenStore()


def get_token_store() -> TokenStore:
    return _store


def set_token_store(store: TokenStore) -> TokenStore:
    """Install a different credential backend and return the previous one."""
    global _store
    previous = _store
    _store = store
    return previous
