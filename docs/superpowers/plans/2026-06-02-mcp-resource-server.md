# MCP Resource Server Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the Hail MCP service as an OAuth 2.1 Resource Server: per-tool-call `HailClient` carrying the user's JWT, pluggable static-key mode for self-host, protected-resource metadata published at the inbound boundary, legacy SSE transport removed.

**Architecture:** Boot-time env dispatch in `mcp/hailhq/mcp/server.py` picks one of two FastMCP configurations: oauth-rs (cloud — `HAIL_AUTH_URL` set, FastMCP `AuthSettings` + pass-through `TokenVerifier`, protected-resource routes auto-mounted by FastMCP) or static-key (self-host — `HAIL_API_KEY` set, no inbound auth, singleton `HailClient`). Tools accept a FastMCP `Context` parameter and use a shared async context manager helper `_client_for(ctx)` that yields a per-call `HailClient` in oauth-rs mode or the singleton in static-key mode — tools are oblivious to the mode.

**Tech Stack:** FastMCP (`mcp.server.fastmcp`), `mcp.server.auth.{provider,settings}` (`TokenVerifier`, `AccessToken`, `AuthSettings`), httpx, pytest + `httpx.ASGITransport`. No new deps.

**Spec:** `docs/superpowers/specs/2026-06-02-mcp-resource-server-design.md`.

---

## Pre-flight

Create the worktree:

```
cd /Users/r/playground/hail
git switch -c feat/mcp-resource-server
```

Run the existing mcp test suite to confirm green-before-changes:

```
cd mcp && uv run pytest -q
```

If anything is red before we start, stop and surface it.

---

### Task 1: Boot-time mode dispatch + tests

**Files:**

- Create: `mcp/hailhq/mcp/auth.py`
- Create: `mcp/tests/test_auth_mode.py`

The mode selector is a small pure function with four cases. Get it covered with tests before adding the verifier, then before touching `server.py`.

- [ ] **Step 1: Write the failing tests.**

Create `mcp/tests/test_auth_mode.py`:

```python
"""Tests for the boot-time MCP auth-mode selector.

The MCP service runs in exactly one of two modes, decided at startup
from the env. The selector is the single source of truth so server.py
and tools.py never branch on env directly.
"""

from __future__ import annotations

import pytest

from hailhq.mcp.auth import AuthMode, select_auth_mode


def test_oauth_rs_mode_when_only_hail_auth_url_set(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
    assert select_auth_mode() is AuthMode.OAUTH_RS


def test_static_key_mode_when_only_hail_api_key_set(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_xxx")
    assert select_auth_mode() is AuthMode.STATIC_KEY


def test_both_set_raises_with_clear_message(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_xxx")
    with pytest.raises(RuntimeError, match="ambiguous MCP auth config"):
        select_auth_mode()


def test_neither_set_raises_with_clear_message(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
    with pytest.raises(RuntimeError, match="MCP auth not configured"):
        select_auth_mode()
```

- [ ] **Step 2: Run the failing tests.**

```
cd mcp && uv run pytest tests/test_auth_mode.py -v
```

Expected: import errors — `hailhq.mcp.auth` does not exist yet.

- [ ] **Step 3: Implement `mcp/hailhq/mcp/auth.py`.**

```python
"""MCP auth — boot-time mode selector + pass-through token verifier.

The MCP service has two operator postures, chosen at startup from env:

* **oauth-rs** — ``HAIL_AUTH_URL`` set. FastMCP gets ``AuthSettings`` and
  a pass-through ``TokenVerifier``; protected-resource metadata is auto-
  mounted by FastMCP. Tools forward each request's JWT to the API.
* **static-key** — ``HAIL_API_KEY`` set. FastMCP runs unauthenticated;
  tools use the shared singleton ``HailClient(api_key=HAIL_API_KEY)``.

The two modes are mutually exclusive — both set is a configuration error
we catch at boot rather than at the first request. The pass-through
verifier exists because ``hail/api`` is the single source of JWT-validation
truth (signature + issuer + audience); MCP's job is to surface the 401 +
``WWW-Authenticate`` discovery hint and to thread the bearer onto the
outbound call.
"""

from __future__ import annotations

import enum

from mcp.server.auth.provider import AccessToken, TokenVerifier

from hailhq.core.config import settings


class AuthMode(enum.Enum):
    OAUTH_RS = "oauth-rs"
    STATIC_KEY = "static-key"


def select_auth_mode() -> AuthMode:
    """Pick the MCP auth mode from env. Raises on ambiguous/missing config.

    Called once at startup by ``server._build_app()``. Tests monkey-patch
    ``settings.hail_auth_url`` and ``settings.hail_api_key`` to exercise
    each branch.
    """
    has_oauth = bool(settings.hail_auth_url)
    has_static = bool(settings.hail_api_key)
    if has_oauth and has_static:
        raise RuntimeError(
            "ambiguous MCP auth config — set HAIL_AUTH_URL XOR HAIL_API_KEY"
        )
    if has_oauth:
        return AuthMode.OAUTH_RS
    if has_static:
        return AuthMode.STATIC_KEY
    raise RuntimeError("MCP auth not configured — set HAIL_AUTH_URL or HAIL_API_KEY")


__all__ = ["AuthMode", "select_auth_mode"]
```

- [ ] **Step 4: Run tests to verify they pass.**

```
cd mcp && uv run pytest tests/test_auth_mode.py -v
```

Expected: 4/4 pass.

- [ ] **Step 5: Commit.**

```
cd /Users/r/playground/hail
git add mcp/hailhq/mcp/auth.py mcp/tests/test_auth_mode.py
git commit -m "feat(mcp): boot-time auth mode selector

The MCP service has two operator postures: oauth-rs (cloud, JWT
forwarded to the API) and static-key (self-host, HAIL_API_KEY).
Mode is decided once at boot from env so server.py and tools.py
never branch on env directly. Both-set and neither-set are
configuration errors caught at startup with clear messages.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: PassThroughVerifier

**Files:**

- Modify: `mcp/hailhq/mcp/auth.py` — append the verifier class
- Modify: `mcp/tests/test_auth_mode.py` — append verifier tests (one file because the verifier is part of the auth module's small surface)

- [ ] **Step 1: Write the failing tests (append to `test_auth_mode.py`).**

```python
import pytest

from hailhq.mcp.auth import PassThroughVerifier


@pytest.mark.asyncio
async def test_pass_through_verifier_accepts_any_non_empty_token():
    v = PassThroughVerifier(resource_server_url="https://mcp.hail.so")
    tok = await v.verify_token("opaque-bearer-value")
    assert tok is not None
    assert tok.token == "opaque-bearer-value"
    assert tok.scopes == []
    # Resource is the audience we expect the API to accept this token for.
    assert tok.resource == "https://mcp.hail.so"


@pytest.mark.asyncio
async def test_pass_through_verifier_rejects_empty_token():
    v = PassThroughVerifier(resource_server_url="https://mcp.hail.so")
    assert await v.verify_token("") is None


@pytest.mark.asyncio
async def test_pass_through_verifier_is_pass_through_no_signature_check():
    """Garbage-shaped tokens still pass — signature/issuer/exp is the API's
    job, not MCP's. This guards against accidentally adding validation here."""
    v = PassThroughVerifier(resource_server_url="https://mcp.hail.so")
    assert await v.verify_token("not-even-jwt-shaped") is not None
    assert await v.verify_token("aaa.bbb.malformed") is not None
```

- [ ] **Step 2: Run tests, expect failure.**

```
cd mcp && uv run pytest tests/test_auth_mode.py -v -k pass_through
```

Expected: `ImportError: cannot import name 'PassThroughVerifier'`.

- [ ] **Step 3: Append the verifier to `mcp/hailhq/mcp/auth.py`.**

Add below `select_auth_mode()`:

```python
class PassThroughVerifier(TokenVerifier):
    """Accept the bearer without validating its signature.

    The API verifies the JWT (signature, issuer, audience, expiry, JWKS) —
    duplicating that here would create a key-rotation race and a second
    source of truth. The verifier's job is to populate request state so
    FastMCP's auth middleware recognises the call as authenticated and so
    tools can read the bearer off ``ctx.request_context``.

    ``resource_server_url`` is the MCP's audience identity (e.g.
    ``https://mcp.hail.so``). FastMCP uses it both for the
    ``WWW-Authenticate: Bearer resource_metadata=...`` header on 401s and
    as the ``resource`` field on the ``AccessToken`` it hands tools.
    """

    def __init__(self, *, resource_server_url: str) -> None:
        self._resource_server_url = resource_server_url

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token:
            return None
        return AccessToken(
            token=token,
            client_id="<opaque>",  # We don't decode the JWT — client_id is unknown.
            scopes=[],
            resource=self._resource_server_url,
        )
```

And add to `__all__`:

```python
__all__ = ["AuthMode", "PassThroughVerifier", "select_auth_mode"]
```

- [ ] **Step 4: Run tests, verify they pass.**

```
cd mcp && uv run pytest tests/test_auth_mode.py -v
```

Expected: 7/7 pass.

- [ ] **Step 5: Commit.**

```
git add mcp/hailhq/mcp/auth.py mcp/tests/test_auth_mode.py
git commit -m "feat(mcp): add pass-through TokenVerifier for oauth-rs mode

The verifier populates FastMCP's request state with the inbound bearer
without validating signature/issuer/audience — hail/api is the single
source of JWT-validation truth. resource_server_url is wired through
so FastMCP can stamp it on WWW-Authenticate headers and on the
AccessToken handed to tools.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Refactor `server.py` — drop SSE, dispatch on mode

**Files:**

- Modify: `mcp/hailhq/mcp/server.py`
- Create: `mcp/tests/test_server_transport.py`

Server now picks one of two FastMCP configurations at boot, and the SSE legacy app is gone.

- [ ] **Step 1: Write the failing tests.**

Create `mcp/tests/test_server_transport.py`:

```python
"""Smoke tests for the server's boot wiring.

In oauth-rs mode: unauth requests get 401 with the protected-resource
hint, and FastMCP auto-mounts /.well-known/oauth-protected-resource.
In static-key mode: no auth, no protected-resource route. SSE is gone
in both modes.
"""

from __future__ import annotations

import importlib
import json

import httpx
import pytest


def _boot(monkeypatch, *, oauth: bool) -> object:
    """Reload server.py under a specific mode env."""
    if oauth:
        monkeypatch.setattr(
            "hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth"
        )
        monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
        monkeypatch.setattr(
            "hailhq.core.config.settings.mcp_resource_url", "https://mcp.hail.so"
        )
    else:
        monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
        monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_test")

    import hailhq.mcp.server as srv

    return importlib.reload(srv)


@pytest.mark.asyncio
async def test_oauth_rs_unauth_returns_401_with_resource_metadata(monkeypatch):
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert resp.status_code == 401
    www = resp.headers.get("www-authenticate", "")
    assert "Bearer" in www
    assert "resource_metadata=" in www
    assert "https://mcp.hail.so/.well-known/oauth-protected-resource" in www


@pytest.mark.asyncio
async def test_oauth_rs_publishes_protected_resource_metadata(monkeypatch):
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/.well-known/oauth-protected-resource")
    assert resp.status_code == 200
    body = resp.json()
    assert body["resource"] == "https://mcp.hail.so"
    assert "https://hail.so/api/auth" in body["authorization_servers"]


@pytest.mark.asyncio
async def test_static_key_no_auth_no_protected_resource_route(monkeypatch):
    srv = _boot(monkeypatch, oauth=False)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # Missing bearer is fine in static-key mode (the route does not require auth).
        # We expect SOMETHING other than 401-with-WWW-Authenticate. A 4xx without
        # the discovery hint is the contract.
        resp = await c.post("/", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        if resp.status_code == 401:
            assert "resource_metadata=" not in resp.headers.get("www-authenticate", "")
        # And the protected-resource route is not mounted.
        wk = await c.get("/.well-known/oauth-protected-resource")
    assert wk.status_code == 404


@pytest.mark.asyncio
async def test_no_sse_routes_in_either_mode(monkeypatch):
    """SSE is gone — neither /sse nor /messages/ is mounted."""
    for oauth in (True, False):
        srv = _boot(monkeypatch, oauth=oauth)
        paths = {
            getattr(r, "path", None) for r in srv.app.routes
        } | {
            getattr(r, "path_format", None) for r in srv.app.routes
        }
        assert "/sse" not in paths, f"oauth={oauth}: /sse should be removed"
        assert "/messages/" not in paths, f"oauth={oauth}: /messages/ should be removed"


@pytest.mark.asyncio
async def test_healthz_works_in_both_modes(monkeypatch):
    for oauth in (True, False):
        srv = _boot(monkeypatch, oauth=oauth)
        transport = httpx.ASGITransport(app=srv.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            resp = await c.get("/healthz")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}
```

- [ ] **Step 2: Run tests, expect failures.**

```
cd mcp && uv run pytest tests/test_server_transport.py -v
```

Expected: at least the 401-WWW-Authenticate test fails (no auth wired) and the protected-resource test 404s. The SSE-removal test currently fails too (SSE is still mounted). Healthz already works.

- [ ] **Step 3: Add `mcp_resource_url` to core settings.**

Edit `core/hailhq/core/config.py` — find the auth-backend block we added earlier and add the MCP resource URL adjacent to `hail_auth_url`:

```python
    # Hail auth backend (cloud) — OAuth/JWT verification alongside the
    # existing API-key path. ``hail_auth_url`` is the issuer URL the auth
    # backend stamps on JWTs (Better Auth's ``ctx.baseURL``, e.g.
    # "https://hail.so/api/auth"); the JWKS endpoint is derived as
    # ``${hail_auth_url}/jwks``. ``hail_auth_audiences`` is a CSV of
    # accepted ``aud`` claims (e.g. "https://api.hail.so,https://mcp.hail.so").
    # Leave both empty in self-host: the JWT path stays disabled and only
    # shared-key + API-key paths are tried.
    hail_auth_url: str = ""
    hail_auth_audiences: str = ""

    # MCP service resource identity (cloud) — the public URL the MCP
    # serves under (e.g. "https://mcp.hail.so"). Used by the MCP server's
    # FastMCP AuthSettings as ``resource_server_url`` so the 401's
    # WWW-Authenticate header points clients at this MCP's own
    # ``.well-known/oauth-protected-resource``. Empty in self-host.
    mcp_resource_url: str = ""
```

- [ ] **Step 4: Refactor `mcp/hailhq/mcp/server.py`.**

Replace the entire file with:

```python
"""Hail MCP server — Streamable HTTP only, mode-dispatched at boot.

The deployable artifact is ``app``. FastMCP serves Streamable HTTP at
``/`` (root — the service runs on a dedicated MCP subdomain so the
endpoint is the bare host URL). ``/healthz`` is mounted on the same
Starlette parent app so the compose healthcheck stays a one-line probe.

Two boot modes (see ``hailhq.mcp.auth.select_auth_mode``):

* **oauth-rs** — FastMCP receives ``AuthSettings`` + a pass-through
  ``TokenVerifier``. FastMCP auto-mounts
  ``/.well-known/oauth-protected-resource`` and rejects bearer-less
  requests with ``401 WWW-Authenticate: Bearer resource_metadata=...``.
* **static-key** — no FastMCP auth, no protected-resource route. Tools
  use the shared ``HAIL_API_KEY`` singleton (unchanged from pre-1c).

Streamable HTTP needs its session manager running for the lifetime of
the app. ``FastMCP.streamable_http_app()`` wires that into its own
Starlette lifespan, but here we own the combined parent app, so we drive
``session_manager.run()`` from the parent lifespan ourselves.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.auth.settings import AuthSettings
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hailhq.core.config import settings
from hailhq.mcp.auth import AuthMode, PassThroughVerifier, select_auth_mode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools


def _build_app() -> tuple[FastMCP, HailClient | None, Starlette]:
    mode = select_auth_mode()

    if mode is AuthMode.OAUTH_RS:
        verifier = PassThroughVerifier(resource_server_url=settings.mcp_resource_url)
        auth_settings = AuthSettings(
            issuer_url=settings.hail_auth_url,
            resource_server_url=settings.mcp_resource_url,
            required_scopes=None,  # scope enforcement deferred to Phase 2
        )
        mcp_app: FastMCP = FastMCP(
            name="hail",
            streamable_http_path="/",
            host="0.0.0.0",
            token_verifier=verifier,
            auth=auth_settings,
        )
        # Tools build per-call HailClient from ctx.request_context bearer;
        # no module-level singleton in oauth-rs mode.
        singleton: HailClient | None = None
    else:
        # static-key: pre-1c shape.
        mcp_app = FastMCP(name="hail", streamable_http_path="/", host="0.0.0.0")
        singleton = HailClient()

    register_tools(mcp_app, mode=mode, singleton=singleton)

    http_app = mcp_app.streamable_http_app()  # / (root)

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.session_manager.run():
            yield

    # Splatting sub_app.routes drops sub_app.user_middleware. FastMCP
    # adds AuthenticationMiddleware + AuthContextMiddleware *inside*
    # streamable_http_app() when auth is configured — those land on
    # http_app.user_middleware, NOT on its routes. Fold them into the
    # parent app's middleware stack so 401-with-WWW-Authenticate fires
    # before any route resolution.
    app = Starlette(
        routes=[Route("/healthz", healthz, methods=["GET"]), *http_app.routes],
        middleware=list(http_app.user_middleware),
        lifespan=lifespan,
    )
    return mcp_app, singleton, app


mcp_app, hail_client, app = _build_app()

__all__ = ["app", "mcp_app", "hail_client"]
```

The signature change of `register_tools` (now takes `mode=` + `singleton=`) is the bridge to Task 4. The tool refactor lands in the next task; this commit will have a transient breakage there which Task 4 fixes — that's why Tasks 3 and 4 ship together if you run the full test suite. Run only `test_server_transport.py` and `test_auth_mode.py` to verify Task 3 in isolation.

- [ ] **Step 5: Run the server-transport tests.**

```
cd mcp && uv run pytest tests/test_server_transport.py tests/test_auth_mode.py -v
```

Expected: all of test_server_transport pass; test_auth_mode still 7/7. The full suite will be temporarily red (tools.py mismatch) until Task 4.

- [ ] **Step 6: Commit.**

```
git add core/hailhq/core/config.py mcp/hailhq/mcp/server.py mcp/tests/test_server_transport.py
git commit -m "feat(mcp): drop SSE, dispatch FastMCP config on auth mode

In oauth-rs mode FastMCP receives AuthSettings + PassThroughVerifier so
the 401 carries WWW-Authenticate: Bearer resource_metadata pointing at
the MCP's own /.well-known/oauth-protected-resource. In static-key mode
the server boots unchanged minus SSE. Adds mcp_resource_url to core
config (cloud env var; empty in self-host). register_tools signature
changes — full suite stays red until the tools refactor lands.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `tools.py` — Context param + async context manager helper

**Files:**

- Modify: `mcp/hailhq/mcp/tools.py`
- Create: `mcp/tests/test_tools_client_for.py`
- Modify: `mcp/tests/test_tools.py` (existing — if any tool-call tests need a Context fixture)

- [ ] **Step 1: Read the existing `tools.py` shape.**

```
cd mcp && cat hailhq/mcp/tools.py | head -50
```

Look at how `place_call_tool`, `send_email_tool`, `get_call_tool`, `list_calls_tool`, `get_events_tool` are wired. They currently close over `client` from `register_tools(mcp_app, client)`. Each is delegated to a free-standing domain function (e.g., `place_call(client=client, ...)`).

- [ ] **Step 2: Write the failing tests for `_client_for`.**

Create `mcp/tests/test_tools_client_for.py`:

```python
"""Tests for the per-tool-call HailClient helper.

In oauth-rs mode the helper builds a fresh HailClient from the bearer
on FastMCP's request context, hands it to the tool, and closes it on
exit. In static-key mode the helper yields the shared singleton without
closing.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import _client_for


def _ctx_with_bearer(bearer: str | None) -> SimpleNamespace:
    """Minimal FastMCP Context stand-in with the headers attribute the
    helper reaches into. Real FastMCP injects a richer Context object;
    we only depend on ``ctx.request_context.request.headers``."""
    headers = {}
    if bearer is not None:
        headers["authorization"] = f"Bearer {bearer}"
    return SimpleNamespace(
        request_context=SimpleNamespace(request=SimpleNamespace(headers=headers))
    )


@pytest.mark.asyncio
async def test_client_for_oauth_rs_builds_from_bearer():
    ctx = _ctx_with_bearer("eyJfake.jwt.value")
    async with _client_for(ctx, mode=AuthMode.OAUTH_RS, singleton=None) as client:
        assert isinstance(client, HailClient)
        # The bearer is wired through the constructor as api_key.
        assert client._api_key == "eyJfake.jwt.value"


@pytest.mark.asyncio
async def test_client_for_oauth_rs_closes_on_exit():
    """The per-call client's httpx pool must close on context exit."""
    ctx = _ctx_with_bearer("opaque")
    async with _client_for(ctx, mode=AuthMode.OAUTH_RS, singleton=None) as client:
        underlying = client._client
    assert underlying.is_closed


@pytest.mark.asyncio
async def test_client_for_oauth_rs_missing_bearer_raises():
    ctx = _ctx_with_bearer(None)
    with pytest.raises(RuntimeError, match="missing Authorization"):
        async with _client_for(ctx, mode=AuthMode.OAUTH_RS, singleton=None):
            pass


@pytest.mark.asyncio
async def test_client_for_static_key_yields_singleton():
    singleton = HailClient(base_url="http://t", api_key="hl_live_xxx")
    ctx = _ctx_with_bearer(None)  # No bearer needed in static-key mode.
    async with _client_for(ctx, mode=AuthMode.STATIC_KEY, singleton=singleton) as client:
        assert client is singleton
    # Singleton stays open after the context exits.
    assert not singleton._client.is_closed
    await singleton.aclose()
```

- [ ] **Step 3: Run, expect import failures.**

```
cd mcp && uv run pytest tests/test_tools_client_for.py -v
```

Expected: ImportError on `_client_for` from `hailhq.mcp.tools`.

- [ ] **Step 4: Refactor `mcp/hailhq/mcp/tools.py`.**

Two changes:

(a) Top of file — add imports + the helper:

```python
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from mcp.server.fastmcp import Context, FastMCP
from pydantic import ValidationError

from hailhq.core.config import settings
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.hail_client import HailAPIError, HailClient

# ... existing _format_api_error, _validation_error_message, and the
# free-standing domain functions (place_call, send_email, get_call,
# list_calls, get_events) stay unchanged.


@contextlib.asynccontextmanager
async def _client_for(
    ctx: Context,
    *,
    mode: AuthMode,
    singleton: HailClient | None,
) -> AsyncIterator[HailClient]:
    """Yield a HailClient appropriate to the active auth mode.

    oauth-rs: build a per-call client from the inbound Authorization
    bearer (a JWT minted by hail-website's Better Auth oauth-provider).
    The client closes its httpx pool on context exit.

    static-key: yield the shared singleton without closing it on exit
    (its httpx pool lives for the lifetime of the process).
    """
    if mode is AuthMode.OAUTH_RS:
        bearer = _bearer_from_ctx(ctx)
        client = HailClient(api_key=bearer)
        try:
            yield client
        finally:
            await client.aclose()
        return

    # static-key
    if singleton is None:  # defensive — server.py wires this
        raise RuntimeError("static-key mode requires a singleton HailClient")
    yield singleton


def _bearer_from_ctx(ctx: Context) -> str:
    headers = ctx.request_context.request.headers
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    if not raw:
        raise RuntimeError("missing Authorization header on MCP request")
    parts = raw.split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
        raise RuntimeError("missing Authorization Bearer token on MCP request")
    return parts[1].strip()
```

(b) Refactor `register_tools` to take `mode` + `singleton` and inject `ctx: Context` into each tool closure. Each tool wraps its delegation in `async with _client_for(ctx, ...)`. Repeat the pattern for all five tools:

```python
def register_tools(
    mcp_app: FastMCP,
    *,
    mode: AuthMode,
    singleton: HailClient | None,
) -> None:
    """Register the five Hail tools on a FastMCP app.

    Tools accept a FastMCP ``Context`` parameter (auto-injected). The
    ``_client_for`` helper picks the right HailClient for the active mode
    — per-tool-call in oauth-rs, shared singleton in static-key.
    """

    @mcp_app.tool(name="place_call")
    async def place_call_tool(
        ctx: Context,
        to: str,
        system_prompt: str | None = None,
        llm: dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """[KEEP THE EXISTING DOCSTRING VERBATIM]"""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await place_call(
                    client=client,
                    to=to,
                    system_prompt=system_prompt,
                    llm=llm,
                    from_=from_,
                    first_message=first_message,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="send_email")
    async def send_email_tool(
        ctx: Context,
        to: list[str],
        subject: str,
        body_text: str | None = None,
        body_html: str | None = None,
        from_: str | None = None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """[KEEP THE EXISTING DOCSTRING VERBATIM]"""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await send_email(
                    client=client,
                    to=to,
                    subject=subject,
                    body_text=body_text,
                    body_html=body_html,
                    from_=from_,
                    cc=cc,
                    bcc=bcc,
                    reply_to=reply_to,
                    metadata=metadata,
                    idempotency_key=idempotency_key,
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_call")
    async def get_call_tool(ctx: Context, call_id: str) -> dict[str, Any]:
        """[KEEP THE EXISTING DOCSTRING VERBATIM]"""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_call(client=client, call_id=call_id)
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="list_calls")
    async def list_calls_tool(
        ctx: Context,
        cursor: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """[KEEP THE EXISTING DOCSTRING VERBATIM]"""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await list_calls(
                    client=client, cursor=cursor, limit=limit, status=status, to=to
                )
        except RuntimeError as exc:
            return {"error": str(exc)}

    @mcp_app.tool(name="get_events")
    async def get_events_tool(
        ctx: Context,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        """[KEEP THE EXISTING DOCSTRING VERBATIM]"""
        try:
            async with _client_for(ctx, mode=mode, singleton=singleton) as client:
                return await get_events(
                    client=client, id=id, kind=kind, cursor=cursor, limit=limit
                )
        except RuntimeError as exc:
            return {"error": str(exc)}
```

(The `[KEEP THE EXISTING DOCSTRING VERBATIM]` markers are instructions to the implementer — copy the existing docstrings from each tool body into the new bodies. They are user-facing tool descriptions and must not change.)

- [ ] **Step 5: Run client-for tests, expect pass.**

```
cd mcp && uv run pytest tests/test_tools_client_for.py -v
```

Expected: 4/4 pass.

- [ ] **Step 6: Run the full mcp suite.**

```
cd mcp && uv run pytest -q
```

Expected: green. If existing `test_tools.py` (or similar) breaks because tool functions now expect a `ctx` arg, add a tiny `Context` fixture/stand-in similar to `_ctx_with_bearer` and pass it through. Match the existing tests' style.

- [ ] **Step 7: Commit.**

```
git add mcp/hailhq/mcp/tools.py mcp/tests/test_tools_client_for.py mcp/tests/test_tools.py
git commit -m "feat(mcp): per-tool-call HailClient threaded through ctx

Tools now accept a FastMCP Context arg. The new _client_for helper
yields a fresh HailClient built from the inbound bearer in oauth-rs
mode, or the singleton in static-key mode. Missing Authorization on an
oauth-rs request surfaces as a structured tool error rather than a
500. Tool docstrings and input shape are unchanged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `.env.example` updates + operations runbook note

**Files:**

- Modify: `mcp/.env.example` (or create if missing)
- Modify: `.env.example` at repo root — clarify the new MCP-side env vars
- Modify: `docs/operations.md` — append a note about MCP modes

- [ ] **Step 1: Update `.env.example` (repo root) with `MCP_RESOURCE_URL`.**

Find the existing `# ─── Auth backend (cloud) ───` block (the one we added in 1a + renamed this session). Append `MCP_RESOURCE_URL` below `HAIL_AUTH_AUDIENCES`:

```
# MCP service resource identity (cloud) — public URL the MCP serves under.
# Used by the MCP server to publish .well-known/oauth-protected-resource and
# to stamp the WWW-Authenticate header on 401s. Cloud example:
#   MCP_RESOURCE_URL=https://mcp.hail.so
# Leave empty in self-host (MCP runs in static-key mode and does not
# publish protected-resource metadata).
MCP_RESOURCE_URL=
```

- [ ] **Step 2: Append the MCP-modes section to `docs/operations.md`.**

Find the "Authentication" section in `docs/operations.md` and add a subsection below it:

```markdown
### MCP modes

The MCP service (`hail/mcp`) picks one of two modes at boot from env:

| Mode           | Env                                                            | Behaviour                                                                                                                                                                                  |
| -------------- | -------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| **oauth-rs**   | `HAIL_AUTH_URL` + `MCP_RESOURCE_URL` set, `HAIL_API_KEY` empty | FastMCP rejects unauth requests with `401 WWW-Authenticate: Bearer resource_metadata=…`; tools forward each request's JWT to the API; `.well-known/oauth-protected-resource` is published. |
| **static-key** | `HAIL_API_KEY` set, `HAIL_AUTH_URL` empty                      | No inbound auth; tools use the singleton `HailClient(api_key=HAIL_API_KEY)`; no protected-resource route.                                                                                  |

Both set → boot fails with `ambiguous MCP auth config`. Neither set → boot fails with `MCP auth not configured`. The mode is decided once and cannot change without restart.

The MCP service does not validate JWT signatures — `hail/api` is the single source of JWT-validation truth (`HAIL_AUTH_URL`, `HAIL_AUTH_AUDIENCES`). MCP forwards the bearer onto the outbound call; the API validates and resolves the org.
```

- [ ] **Step 3: Commit.**

```
git add .env.example docs/operations.md
git commit -m "docs(mcp): document oauth-rs and static-key modes

Adds MCP_RESOURCE_URL to .env.example and a short runbook section
explaining how the MCP service picks its auth mode at boot, and that
JWT signature verification stays in the API.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Final integration check

**Files:** none — verification only.

- [ ] **Step 1: Run the full mcp suite under both modes.**

```
cd mcp && uv run pytest -q
```

Expected: all green.

- [ ] **Step 2: Run the api suite for regression.**

```
cd /Users/r/playground/hail/api && uv run pytest -q
```

Expected: still 138/138 (1a's tests are unaffected, but smoke as a precaution).

- [ ] **Step 3: Optional manual smoke (skip if no Postgres + hail-website running).**

```
# Boot the MCP in oauth-rs mode against a local hail-website + API.
cd /Users/r/playground/hail/mcp
HAIL_AUTH_URL=http://localhost:3000/api/auth \
HAIL_AUTH_AUDIENCES=http://localhost:8081 \
MCP_RESOURCE_URL=http://localhost:8081 \
HAIL_API_URL=http://localhost:8080 \
uv run uvicorn hailhq.mcp.server:app --port 8081
```

In another terminal:

```
curl -sv -X POST http://localhost:8081/ \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}' 2>&1 | grep -i "www-authenticate\|HTTP/"
```

Expected: `HTTP/1.1 401 Unauthorized` and `WWW-Authenticate: Bearer resource_metadata=http://localhost:8081/.well-known/oauth-protected-resource`.

```
curl -s http://localhost:8081/.well-known/oauth-protected-resource | jq .
```

Expected: `{"resource": "http://localhost:8081", "authorization_servers": ["http://localhost:3000/api/auth"]}` (plus whatever fields FastMCP adds — the assertions in Task 3's test are non-exhaustive).

- [ ] **Step 4: Final-review handoff.**

Hand the branch to `superpowers:requesting-code-review` and then `superpowers:finishing-a-development-branch`. No additional commit at this stage; the verification above is informational.

---

## Self-Review

**Spec coverage** (every load-bearing element of `docs/superpowers/specs/2026-06-02-mcp-resource-server-design.md`):

- Mode dispatch at boot, mutually exclusive, both/neither errors → Task 1.
- Pass-through `TokenVerifier` (no signature check) → Task 2.
- Drop SSE entirely → Task 3.
- FastMCP `AuthSettings` + auto-mounted protected-resource route → Task 3.
- 401 + `WWW-Authenticate: Bearer resource_metadata=...` → Task 3 (asserted).
- Per-tool-call `HailClient` carrying the inbound bearer → Task 4.
- Static-key singleton path preserved → Task 4 (test asserts singleton identity).
- Missing/malformed bearer surfaces as a structured tool error, not a 500 → Task 4 (RuntimeError caught and returned as `{"error": ...}`).
- `MCP_RESOURCE_URL` settings field added → Task 3.
- `.env.example` + operations runbook updated → Task 5.
- No scope enforcement in 1c → Task 3 (`required_scopes=None`), spec-decided.

**Placeholder scan:** No TBDs. The `[KEEP THE EXISTING DOCSTRING VERBATIM]` markers in Task 4 are instructions, not placeholders — the implementer copies the existing docstrings (the user-facing tool descriptions) verbatim.

**Type / name consistency:**

- `AuthMode` enum values: `OAUTH_RS`, `STATIC_KEY` — used uniformly in Tasks 1-4.
- `PassThroughVerifier(resource_server_url=...)` matches FastMCP's `AuthSettings.resource_server_url` field.
- `_client_for(ctx, *, mode, singleton)` signature is consistent across `tools.py` callers in Task 4 and the test in Task 4 Step 2.
- `register_tools(mcp_app, *, mode, singleton)` consistent between `server.py` (Task 3) and `tools.py` (Task 4).

**Known follow-ups (NOT in this plan):**

- Docs+website cards refresh (`hail/docs/setup/mcp.md`, `hail-website/app/mcp/clients.ts`, homepage `CodePanel.tsx`) — separate plan after 1c.
- Per-tool scope enforcement → Phase 2.
- Audit logging of MCP-mediated API calls (per-org request log surface) → Phase 2.
