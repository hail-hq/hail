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

"initialize" additionally carries a per-remote-IP rate cap (see
_ANON_INIT_MAX_PER_WINDOW below) — unlike tools/list, a real initialize
call creates a permanent, stateful MCP session, so unrestricted anonymous
initialize is a resource-exhaustion vector, not just an information leak.
"""

from __future__ import annotations

import asyncio
import json
import time

from starlette.types import ASGIApp, Receive, Scope, Send

_DISCOVERY_METHODS = frozenset({"initialize", "tools/list"})

# 64 KiB — comfortably above any real initialize/tools/list payload (both are
# small JSON-RPC envelopes; neither carries bulk data), and small enough to
# bound worst-case memory for this pre-auth body peek. Before this middleware
# existed, FastMCP's auth layer 401'd before ever reading the body, so an
# unbounded buffer here would be a new, unauthenticated DoS surface. Once a
# request's body exceeds this, it cannot be a legitimate discovery request —
# stop buffering and fall through with no header injected, so it hits
# FastMCP's real auth middleware unmodified and 401s like any other
# non-safelisted request today.
_MAX_PEEK_BYTES = 64 * 1024

# Bounds wall-clock time, not just memory: without this, a client that
# trickles the body in small chunks under _MAX_PEEK_BYTES and never sets
# more_body=False keeps this coroutine (and its connection) blocked on
# receive() indefinitely — a Slowloris-style hang the byte cap alone does
# not catch. 5s is generous for a same-request body that's supposed to be a
# small JSON-RPC envelope arriving in one or two chunks.
_MAX_PEEK_SECONDS = 5.0

# Anonymous "initialize" rate cap. FastMCP's session manager (this SDK
# version) has no idle timeout — session_idle_timeout defaults to None and
# is never passed — so sessions are reaped only on explicit DELETE, crash,
# or restart. A real "initialize" call through this safelist creates a
# real, permanent, stateful session. Without a cap, an unauthenticated
# caller can loop "initialize" and accumulate unbounded sessions/tasks — a
# resource-exhaustion vector distinct from _MAX_PEEK_BYTES above (that
# bounds per-request memory, not session count). Deliberately NOT applied
# to "tools/list" — that method doesn't create a session, it only reads
# one that already exists.
#
# Self-contained in-memory fixed-window counter keyed by remote IP —
# approximate is fine, this only needs to bound the worst case, not be
# perfectly precise (same reasoning as the body-peek cap; this package has
# no rate-limiting dependency the way api/'s ratelimit.py does, so a bare
# dict is simpler than adding one for a single counter).
_ANON_INIT_WINDOW_SECONDS = 60.0
_ANON_INIT_MAX_PER_WINDOW = 20

_anon_init_counts: dict[str, tuple[int, float]] = {}


def _remote_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _anon_init_cap_exceeded(remote_ip: str) -> bool:
    now = time.monotonic()
    count, window_start = _anon_init_counts.get(remote_ip, (0, now))
    if now - window_start >= _ANON_INIT_WINDOW_SECONDS:
        count, window_start = 0, now
    count += 1
    _anon_init_counts[remote_ip] = (count, window_start)
    return count > _ANON_INIT_MAX_PER_WINDOW


def _reset_anon_init_rate_state_for_tests() -> None:
    """Test-only: clear accumulated per-IP counters between test cases so
    one test's anonymous initialize calls can't trip the cap for another
    (see tests/conftest.py's autouse fixture)."""
    _anon_init_counts.clear()


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
        oversized = False
        deadline = time.monotonic() + _MAX_PEEK_SECONDS
        while more_body:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # Same fallthrough as the oversized case: stop waiting on
                # this client's body and let the real auth middleware 401
                # the request unmodified.
                oversized = True
                break
            try:
                message = await asyncio.wait_for(receive(), timeout=remaining)
            except asyncio.TimeoutError:
                oversized = True
                break
            messages.append(message)
            body += message.get("body", b"")
            more_body = message.get("more_body", False)
            if len(body) > _MAX_PEEK_BYTES:
                # Stop reading now — do not keep draining the client's
                # body into memory just to discover it can never match
                # the safelist. Whatever's left unread stays unread here;
                # replay_receive()'s fallback to the real receive() below
                # still hands it to the downstream app in order, so the
                # ASGI message stream stays coherent.
                oversized = True
                break

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

        method = None
        if not oversized:
            try:
                payload = json.loads(body) if body else {}
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {}
            method = payload.get("method") if isinstance(payload, dict) else None

        cap_exceeded = method == "initialize" and _anon_init_cap_exceeded(
            _remote_ip(scope)
        )
        if method in _DISCOVERY_METHODS and not cap_exceeded:
            new_headers = list(scope.get("headers", []))
            new_headers.append((b"authorization", b"Bearer anonymous-discovery"))
            scope = {**scope, "headers": new_headers}
        # cap_exceeded (or method not in the safelist): no header injected,
        # falls through to FastMCP's real auth middleware unmodified — same
        # established pattern as the oversized-body case above (no bespoke
        # response shape invented here).

        await self.app(scope, replay_receive, send)
