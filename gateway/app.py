"""The gateway demo ASGI app: OAuth onboarding pages + tenant-injecting MCP dispatch.

Layout (mirrors docs/hosted_gateway_design.md):

- ``/``               landing page with the "Connect Moneybird" button
- ``/oauth/login``    generates CSRF state, redirects to Moneybird's consent page
- ``/oauth/callback`` validates state, exchanges the code, creates the user,
                      shows the personal MCP endpoint URL (once)
- ``/u/<key>/mcp``    MCP endpoint: the gateway key is looked up, any
                      client-supplied tenant headers are stripped, and
                      ``X-Moneybird-Token`` / ``X-Moneybird-Administration-Id``
                      are injected before the request reaches the MCP app.
"""
from __future__ import annotations

import html
import json
import re
import secrets
import time
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from moneybird import oauth
from moneybird.client import MoneybirdClient
from moneybird.config import MoneybirdError, data_dir

# A pending OAuth state is valid this long between /oauth/login and the callback.
STATE_TTL_SECONDS = 600

USERS_FILENAME = "gateway_demo_users.json"

_KEY_PATH_PATTERN = re.compile(r"^/u/([A-Za-z0-9_-]{16,})(/mcp.*)$")

TENANT_HEADERS = (b"x-moneybird-token", b"x-moneybird-administration-id")


class GatewayStore:
    """Users of the demo gateway: gateway key -> OAuth profile + administration.

    Plaintext JSON in the data dir — acceptable for the localhost demo only;
    the M2 deployment replaces this with an encrypted store.
    """

    def __init__(self) -> None:
        self._pending_states: dict[str, float] = {}

    def _users_path(self) -> Path:
        return data_dir() / USERS_FILENAME

    def _load_users(self) -> dict[str, dict[str, Any]]:
        path = self._users_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    def _save_users(self, users: dict[str, dict[str, Any]]) -> None:
        self._users_path().write_text(
            json.dumps(users, indent=2, sort_keys=True), encoding="utf-8"
        )

    # --- OAuth state (CSRF) ----------------------------------------------------

    def issue_state(self) -> str:
        state = oauth.generate_state()
        self._pending_states[state] = time.time()
        return state

    def consume_state(self, state: str) -> bool:
        """True when ``state`` was issued by us, is fresh, and is now used up."""
        issued_at = self._pending_states.pop(state, None)
        return issued_at is not None and time.time() - issued_at <= STATE_TTL_SECONDS

    # --- Users ------------------------------------------------------------------

    def create_user(
        self,
        tokens: dict[str, Any],
        administrations: list[dict[str, Any]],
    ) -> dict[str, Any]:
        user_id = secrets.token_hex(4)
        gateway_key = secrets.token_urlsafe(32)
        profile = f"gateway-{user_id}"
        oauth.store_tokens(tokens, profile=profile)
        first = administrations[0] if administrations else {}
        record = {
            "user_id": user_id,
            "profile": profile,
            "administration_id": str(first.get("id", "")) or None,
            "administration_name": first.get("name"),
            "administration_count": len(administrations),
            "created_at": int(time.time()),
        }
        users = self._load_users()
        users[gateway_key] = record
        self._save_users(users)
        return {**record, "gateway_key": gateway_key}

    def lookup(self, gateway_key: str) -> dict[str, Any] | None:
        return self._load_users().get(gateway_key)

    def user_count(self) -> int:
        return len(self._load_users())


# --- Onboarding pages ----------------------------------------------------------


def _page(title: str, body: str) -> HTMLResponse:
    return HTMLResponse(
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{html.escape(title)}</title>"
        "<style>body{font-family:system-ui;max-width:40rem;margin:3rem auto;"
        "line-height:1.5;padding:0 1rem}code{background:#eee;padding:.15rem .35rem;"
        "border-radius:4px;word-break:break-all}</style></head>"
        f"<body><h1>{html.escape(title)}</h1>{body}</body></html>"
    )


def _make_routes(store: GatewayStore) -> list[Route]:
    async def landing(request: Request) -> HTMLResponse:
        count = store.user_count()
        return _page(
            "Moneybird MCP gateway (localhost demo)",
            f"<p>Connected users: {count}. Connecting authorizes this gateway with "
            "your Moneybird account and gives you a personal MCP endpoint URL for "
            "your AI client. Your Moneybird token stays on this machine.</p>"
            "<p><a href='/oauth/login'><strong>Connect Moneybird</strong></a></p>",
        )

    async def oauth_login(request: Request) -> Response:
        state = store.issue_state()
        redirect_uri = str(request.base_url) + "oauth/callback"
        return RedirectResponse(
            oauth.build_authorize_url(redirect_uri=redirect_uri, state=state),
            status_code=302,
        )

    async def oauth_callback(request: Request) -> Response:
        state = request.query_params.get("state", "")
        if not state or not store.consume_state(state):
            return _page(
                "Login failed",
                "<p>The login attempt is unknown or expired (possible CSRF). "
                "Start again from <a href='/'>the landing page</a>.</p>",
            )
        try:
            code = oauth.parse_authorization_callback(
                str(request.url), expected_state=state
            )
            redirect_uri = str(request.base_url) + "oauth/callback"
            tokens = oauth.exchange_authorization_code(code, redirect_uri=redirect_uri)
            client = MoneybirdClient(
                tokens["access_token"], None, require_administration=False
            )
            administrations = client.list_administrations()
        except MoneybirdError as exc:
            return _page("Login failed", f"<p>{html.escape(str(exc))}</p>")
        user = store.create_user(tokens, administrations)
        endpoint = str(request.base_url) + f"u/{user['gateway_key']}/mcp"
        picked = html.escape(str(user.get("administration_name") or "none found"))
        extra = (
            f"<p>Note: this account has {user['administration_count']} administrations; "
            "the demo picked the first one.</p>"
            if user["administration_count"] > 1
            else ""
        )
        return _page(
            "Connected!",
            f"<p>Administration: <strong>{picked}</strong>.</p>{extra}"
            "<p>Your personal MCP endpoint (shown only once — treat it like a "
            f"password):</p><p><code>{html.escape(endpoint)}</code></p>"
            "<p>Add it to your AI client as a streamable-HTTP MCP server.</p>",
        )

    return [
        Route("/", landing),
        Route("/oauth/login", oauth_login),
        Route("/oauth/callback", oauth_callback),
    ]


# --- Tenant-injecting dispatcher -------------------------------------------------


class GatewayDispatcher:
    """Top-level ASGI app: routes ``/u/<key>/mcp`` into the MCP app with tenant
    headers injected; everything else (and lifespan) goes to the pages app."""

    def __init__(self, pages_app: Any, mcp_app: Any, store: GatewayStore) -> None:
        self.pages_app = pages_app
        self.mcp_app = mcp_app
        self.store = store

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.pages_app(scope, receive, send)
            return
        match = _KEY_PATH_PATTERN.match(scope.get("path", ""))
        if match is None:
            await self.pages_app(scope, receive, send)
            return

        gateway_key, inner_path = match.groups()
        record = self.store.lookup(gateway_key)
        token = None
        if record is not None:
            # get_access_token refreshes (and persists) an expired token in passing.
            token = oauth.get_access_token(profile=record["profile"])
        if record is None or not token:
            await _send_plain(send, 401, "Unknown or revoked gateway key.")
            return

        # Never trust client-supplied tenant headers; the key IS the identity.
        headers = [
            (name, value)
            for name, value in scope.get("headers", [])
            if name.lower() not in TENANT_HEADERS
        ]
        headers.append((b"x-moneybird-token", token.encode("utf-8")))
        if record.get("administration_id"):
            headers.append(
                (b"x-moneybird-administration-id", record["administration_id"].encode("utf-8"))
            )

        inner_scope = dict(scope)
        inner_scope["path"] = inner_path
        inner_scope["raw_path"] = inner_path.encode("utf-8")
        inner_scope["headers"] = headers
        await self.mcp_app(inner_scope, receive, send)


async def _send_plain(send: Any, status: int, text: str) -> None:
    body = text.encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"text/plain; charset=utf-8"),
                (b"content-length", str(len(body)).encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_gateway_app(mcp_app: Any | None = None) -> GatewayDispatcher:
    """The complete demo app. ``mcp_app`` is injectable for tests; by default the
    real Moneybird MCP streamable-HTTP app is mounted in-process."""
    if mcp_app is None:
        os.environ.setdefault(
            "MONEYBIRD_TOOL_DISCOVERY",
            os.environ.get("MCP_TOOL_DISCOVERY", "search"),
        )
        from moneybird.tools import mcp

        mcp_app = mcp.http_app(transport="http")
    store = GatewayStore()
    pages_app = Starlette(
        routes=_make_routes(store),
        lifespan=getattr(mcp_app, "lifespan", None),
    )
    return GatewayDispatcher(pages_app, mcp_app, store)
