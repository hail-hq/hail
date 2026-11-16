"""Lets an unauthenticated MCP client discover Hail's capabilities.

MCPServer's AuthenticationMiddleware gates the entire Streamable HTTP
endpoint — every JSON-RPC method, not just tool calls. That's correct for
tools/call (a real action against a real account) but means an agent
evaluating whether to bother authenticating at all can't even see what
tools exist, matching the "properly scoped, upgrade to public tool
listing" gap an external audit flagged.

This middleware runs BEFORE MCPServer's own auth middleware (see server.py's
middleware ordering) and, for exactly these JSON-RPC methods —
"initialize", "server/discover", "tools/list", "resources/list",
"resources/templates/list", "prompts/list" and "notifications/initialized" —
injects a synthetic, non-functional bearer token if the request has none.
The server registers no resources or prompts, but the SDK advertises both
capabilities, so their list methods answer anonymously with empty lists, the
same as tools/list answers with the tool list.
auth.PassThroughVerifier accepts any non-empty token string as "valid" (real validation is the downstream
API's job, and these methods never call the downstream API), so
MCPServer's auth layer then lets the request through.

Every other method (tools/call, resources/read, other notifications/*, ping, ...)
is untouched: no header is injected, so a request with no real bearer
401s exactly as it did before this file existed. This is a safelist, not
a bypass — widening it later needs the same scrutiny as the original
entries, not a one-line addition.

"initialize" additionally carries a per-remote-IP rate cap (see
_ANON_INIT_MAX_PER_WINDOW below) — unlike tools/list, a real initialize
call creates a permanent, stateful MCP session, so unrestricted anonymous
initialize is a resource-exhaustion vector, not just an information leak.
Every safelisted method except "initialize" is only
safelisted when they carry an Mcp-Session-Id header or a 2026-07-28 (or later)
MCP-Protocol-Version header (see _SESSION_REQUIRED_METHODS): a session-less
POST on an older protocol makes the SDK create a session before it answers
400, while a newer-protocol POST is one self-contained exchange that creates
no session. Every MCP client on an older protocol sends
notifications/initialized right after initialize, so it must pass anonymously
or the SDK client fails in connect(). "server/discover" is how a 2026-07-28
client learns the server's name, title and capabilities, the way older
clients use "initialize".
"""

from __future__ import annotations

import asyncio
import json
import time

from starlette.types import ASGIApp, Receive, Scope, Send

from mcp.server.streamable_http import MCP_SESSION_ID_HEADER
from mcp.shared.inbound import MCP_PROTOCOL_VERSION_HEADER
from mcp.types.version import HANDSHAKE_PROTOCOL_VERSIONS

_LIST_METHODS = frozenset(
    {"tools/list", "resources/list", "resources/templates/list", "prompts/list"}
)
_DISCOVERY_METHODS = frozenset(
    {"initialize", "server/discover", "notifications/initialized", *_LIST_METHODS}
)

# Safelisted methods that must not open a session. On an older protocol the
# SDK's session manager mints a new transport for ANY POST that lacks an
# Mcp-Session-Id header, before it answers 400 — so a session-less list call,
# server/discover or notifications/initialized would create a session while
# bypassing the initialize cap below. These pass only with that header, or on
# the newer protocol (_opens_no_session), whose requests never create a
# session. Otherwise they fall through to MCPServer's auth middleware and
# 401, exactly as before this file existed.
_SESSION_REQUIRED_METHODS = _DISCOVERY_METHODS - {"initialize"}

# 64 KiB — comfortably above any real initialize/tools/list payload (both are
# small JSON-RPC envelopes; neither carries bulk data), and small enough to
# bound worst-case memory for this pre-auth body peek. Before this middleware
# existed, MCPServer's auth layer 401'd before ever reading the body, so an
# unbounded buffer here would be a new, unauthenticated DoS surface. Once a
# request's body exceeds this, it cannot be a legitimate discovery request —
# stop buffering and fall through with no header injected, so it hits
# MCPServer's real auth middleware unmodified and 401s like any other
# non-safelisted request today.
_MAX_PEEK_BYTES = 64 * 1024

# Bounds wall-clock time, not just memory: without this, a client that
# trickles the body in small chunks under _MAX_PEEK_BYTES and never sets
# more_body=False keeps this coroutine (and its connection) blocked on
# receive() indefinitely — a Slowloris-style hang the byte cap alone does
# not catch. 5s is generous for a same-request body that's supposed to be a
# small JSON-RPC envelope arriving in one or two chunks.
_MAX_PEEK_SECONDS = 5.0

# Anonymous "initialize" rate cap. MCPServer's session manager (mcp 2)
# reaps a session only after 30 idle minutes, and an open GET stream holds
# it open past that (server.py turns its session count limit off). A real
# "initialize" call through this safelist creates a real, stateful session.
# Without a cap, an unauthenticated caller can loop "initialize" and pile up
# sessions faster than they expire — a resource-exhaustion vector distinct
# from _MAX_PEEK_BYTES above (that bounds per-request memory, not session
# count). Not applied to _SESSION_REQUIRED_METHODS: they are only safelisted
# with an Mcp-Session-Id header or on the newer protocol, so they cannot
# create a session. The cap only
# bounds requests with no Authorization header — PassThroughVerifier accepts
# any non-empty bearer, so it guards against accidental floods, not against
# a client that adds a junk Authorization header.
#
# Self-contained in-memory fixed-window counter keyed by remote IP —
# approximate is fine, this only needs to bound the worst case, not be
# perfectly precise (same reasoning as the body-peek cap; this package has
# no rate-limiting dependency the way api/'s ratelimit.py does, so a bare
# dict is simpler than adding one for a single counter).
_ANON_INIT_WINDOW_SECONDS = 60.0
_ANON_INIT_MAX_PER_WINDOW = 20
_ANON_INIT_PRUNE_THRESHOLD = 1024

_anon_init_counts: dict[str, tuple[int, float]] = {}


def _header(scope: Scope, name: str) -> bytes | None:
    # Exact match on the raw name, as the SDK session manager does.
    key = name.encode("ascii")
    return next((v for k, v in scope.get("headers", []) if k == key), None)


def _opens_no_session(scope: Scope) -> bool:
    """True when the SDK serves this POST as one exchange with no session.

    Mirrors the SDK session manager's routing: a request whose
    MCP-Protocol-Version header names anything but an initialize-handshake
    version goes to the 2026-07-28 single-exchange handler.
    """
    version = _header(scope, MCP_PROTOCOL_VERSION_HEADER)
    return (
        version is not None
        and version.decode("latin-1") not in HANDSHAKE_PROTOCOL_VERSIONS
    )


def _remote_ip(scope: Scope) -> str:
    client = scope.get("client")
    return client[0] if client else "unknown"


def _anon_init_cap_exceeded(remote_ip: str) -> bool:
    now = time.monotonic()
    if len(_anon_init_counts) > _ANON_INIT_PRUNE_THRESHOLD:
        # Drop windows that already expired so distinct source addresses do
        # not accumulate for the life of the process.
        for ip in [
            ip
            for ip, (_, start) in _anon_init_counts.items()
            if now - start >= _ANON_INIT_WINDOW_SECONDS
        ]:
            del _anon_init_counts[ip]
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
    replay the request body without interfering with MCPServer's own
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

        body = bytearray()
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
            # sse_starlette's EventSourceResponse, used by MCPServer's
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
            except (ValueError, RecursionError):
                # JSONDecodeError and UnicodeDecodeError are ValueErrors; an
                # over-long integer literal raises a plain ValueError and a
                # deeply nested body raises RecursionError. None of these may
                # turn an unauthenticated request into a 500 — fall through
                # to MCPServer's auth so it 401s as before.
                payload = {}
            raw_method = payload.get("method") if isinstance(payload, dict) else None
            # A list/dict "method" is unhashable and would raise TypeError in
            # the frozenset membership test below.
            method = raw_method if isinstance(raw_method, str) else None

        if (
            method in _SESSION_REQUIRED_METHODS
            and _header(scope, MCP_SESSION_ID_HEADER) is None
            and not _opens_no_session(scope)
        ):
            method = None
        cap_exceeded = method == "initialize" and _anon_init_cap_exceeded(
            _remote_ip(scope)
        )
        if method in _DISCOVERY_METHODS and not cap_exceeded:
            new_headers = list(scope.get("headers", []))
            new_headers.append((b"authorization", b"Bearer anonymous-discovery"))
            scope = {**scope, "headers": new_headers}
        # cap_exceeded (or method not in the safelist): no header injected,
        # falls through to MCPServer's real auth middleware unmodified — same
        # established pattern as the oversized-body case above (no bespoke
        # response shape invented here).

        await self.app(scope, replay_receive, send)
