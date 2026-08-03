"""Optional shared-secret ASGI auth middleware for the SSE endpoint."""
from __future__ import annotations

import secrets
from typing import Any


class SharedSecretAuthMiddleware:
    """ASGI middleware that rejects requests lacking the shared secret.

    The secret may be supplied either as ``Authorization: Bearer <token>`` or as
    an ``X-MCP-Token`` header. Comparison is constant-time. Non-HTTP scopes
    (e.g. lifespan) pass through untouched.
    """

    def __init__(self, app: Any, token: str) -> None:
        self.app = app
        self.token = token

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers") or []}
        provided = ""
        authorization = headers.get(b"authorization", b"").decode("latin-1")
        if authorization[:7].lower() == "bearer ":
            provided = authorization[7:].strip()
        if not provided:
            provided = headers.get(b"x-mcp-token", b"").decode("latin-1").strip()

        if not (provided and secrets.compare_digest(provided, self.token)):
            body = b'{"error":"unauthorized"}'
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        await self.app(scope, receive, send)
