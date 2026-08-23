"""Lets an unauthenticated MCP client discover Hail's capabilities.

FastMCP's AuthenticationMiddleware gates the entire Streamable HTTP
endpoint — every JSON-RPC method, not just tool calls. That's correct for
tools/call (a real action against a real account) but means an agent
evaluating whether to bother authenticating at all can't even see what
tools exist, matching the "properly scoped, upgrade to public tool
listing" gap an external audit flagged.

This middleware runs BEFORE FastMCP's own auth middleware (see server.py's
middleware ordering) and, for exactly two JSON-RPC methods —
"initialize" and "tools/list" — injects a synthetic, non-functional
bearer token if the request has none. auth.PassThroughVerifier accepts
any non-empty token string as "valid" (real validation is the downstream
API's job, and these two methods never call the downstream API), so
FastMCP's auth layer then lets the request through.

Every other method (tools/call, resources/*, notifications/*, ping, ...)
is untouched: no header is injected, so a request with no real bearer
401s exactly as it did before this file existed. This is a safelist, not
a bypass — widening it later needs the same scrutiny as the original
two entries, not a one-line addition.
"""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Receive, Scope, Send

_DISCOVERY_METHODS = frozenset({"initialize", "tools/list"})


class DiscoveryAuthMiddleware:
    """Pure ASGI middleware (not BaseHTTPMiddleware) so it can inspect and
    replay the request body without interfering with FastMCP's own
    body-consuming logic downstream — BaseHTTPMiddleware's buffering has
    known interactions with streaming bodies that this avoids."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        has_auth = any(
            k.lower() == b"authorization" for k, _ in scope.get("headers", [])
        )
        if has_auth:
            await self.app(scope, receive, send)
            return

        body = b""
        more_body = True
        messages = []
        while more_body:
            message = await receive()
            messages.append(message)
            body += message.get("body", b"")
            more_body = message.get("more_body", False)

        async def replay_receive() -> dict:
            # Replay the buffered body messages first; once exhausted,
            # delegate to the REAL upstream receive() rather than
            # fabricating {"type": "http.disconnect"}. Downstream (e.g.
            # sse_starlette's EventSourceResponse, used by FastMCP's
            # streamable HTTP transport) has its own disconnect-watcher
            # task that calls receive() past body-consumption expecting
            # it to reflect real connection state. A fabricated disconnect
            # here makes that watcher fire immediately, which cancels the
            # whole SSE response's task group before the actual JSON-RPC
            # payload is ever streamed back — confirmed empirically: the
            # client saw 200 + SSE headers but a truncated/empty body.
            if messages:
                return messages.pop(0)
            return await receive()

        try:
            payload = json.loads(body) if body else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            payload = {}

        method = payload.get("method") if isinstance(payload, dict) else None
        if method in _DISCOVERY_METHODS:
            new_headers = list(scope.get("headers", []))
            new_headers.append((b"authorization", b"Bearer anonymous-discovery"))
            scope = {**scope, "headers": new_headers}

        await self.app(scope, replay_receive, send)
