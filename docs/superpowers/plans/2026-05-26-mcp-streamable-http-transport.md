# MCP Streamable HTTP Transport Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Hail MCP server serve Streamable HTTP at `/mcp` (the current MCP transport) alongside the existing legacy SSE at `/sse`, with no behavior change to the five tools.

**Architecture:** `mcp/hailhq/mcp/server.py` currently builds one Starlette app from `FastMCP.sse_app()` and tacks on `/healthz`. We change it to build _both_ transport apps (`sse_app()` → `/sse` + `/messages/`; `streamable_http_app()` → `/mcp`), combine their route lists plus `/healthz` into one parent Starlette, and drive the Streamable HTTP session manager from that parent app's lifespan. `app` stays the uvicorn entrypoint; `/sse` stays mounted as a transition path.

**Tech Stack:** Python 3.11, `mcp` 1.27.0 (`FastMCP`), Starlette 1.0.0, pytest + pytest-asyncio (`asyncio_mode = auto`), Starlette `TestClient`.

**Spec:** `docs/superpowers/specs/2026-05-26-mcp-server-roadmap-design.md` (Phase 0a).

---

## Why the lifespan matters (read before Task 1)

`FastMCP.streamable_http_app()` (mcp 1.27.0, `server.py:950`) lazily creates a `StreamableHTTPSessionManager` and returns a Starlette app whose lifespan is `lambda app: self.session_manager.run()` (line 1044). That session manager **must be running** for `/mcp` to serve requests. When you let FastMCP own the whole app, it wires this for you. We instead build our _own_ parent Starlette (to also serve `/sse` and `/healthz`), so **we** must run `session_manager.run()` from the parent lifespan. Forgetting this makes `/mcp` fail at request time even though the route exists. `session_manager.run()` is an `@asynccontextmanager` and may only be entered once per `FastMCP` instance — so tests build a fresh app via `_build_app()` rather than reusing the module-level singleton.

## File Structure

- **Modify** `mcp/hailhq/mcp/server.py` — build the combined Starlette app (SSE + Streamable HTTP + `/healthz`) with the session-manager lifespan. One responsibility: assemble the deployable ASGI `app`.
- **Create** `mcp/tests/test_server.py` — transport-wiring tests (route presence + lifespan startup). The MCP protocol handshake stays out of scope, matching the existing `test_tools.py` posture.
- **Modify** `docs/setup/mcp.md` — document `/mcp` (Streamable HTTP) as the primary endpoint, `/sse` as the legacy transition path.

No changes needed to `Caddyfile` (it reverse-proxies the whole `mcp.${HAIL_DOMAIN}` host to `mcp:8081`, so `/mcp` is proxied automatically; `flush_interval -1` is correct for streaming HTTP too) or to `docker-compose*.yml` (the healthcheck hits `/healthz`, which we keep).

---

### Task 1: Combined transport app + wiring tests

**Files:**

- Modify: `mcp/hailhq/mcp/server.py`
- Create: `mcp/tests/test_server.py`

- [ ] **Step 1: Write the failing route-presence test**

Create `mcp/tests/test_server.py`:

```python
"""Transport-level wiring tests for the combined MCP Starlette app.

These assert the deployable ``app`` exposes both MCP transports
(Streamable HTTP at ``/mcp``, legacy SSE at ``/sse`` + ``/messages/``)
plus the ``/healthz`` probe, and that the Streamable HTTP
session-manager lifespan starts and stops cleanly. The MCP protocol
handshake itself is framework territory and is not covered here —
matching the posture of ``test_tools.py``.

Each test builds a fresh app via ``_build_app()`` rather than importing
the module-level singleton: ``StreamableHTTPSessionManager.run()`` may
only be entered once per ``FastMCP`` instance, so a fresh app per test
keeps the lifespan re-entrant across the suite.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from hailhq.mcp.server import _build_app


def _route_paths(app: Starlette) -> set[str | None]:
    return {getattr(route, "path", None) for route in app.routes}


def test_app_exposes_both_transports_and_healthz() -> None:
    _mcp_app, _client, app = _build_app()
    paths = _route_paths(app)
    assert "/healthz" in paths
    assert "/sse" in paths
    assert "/mcp" in paths
    # SSE delivers client->server messages to a mounted message path.
    assert any(p is not None and p.startswith("/messages") for p in paths)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd mcp && uv run pytest tests/test_server.py::test_app_exposes_both_transports_and_healthz -v`
Expected: FAIL — `assert "/mcp" in paths` is false, because the current `_build_app()` returns only the SSE app (`/sse`, `/messages/`, `/healthz`); there is no `/mcp` route yet.

- [ ] **Step 3: Rewrite `server.py` to build the combined app**

Replace the entire contents of `mcp/hailhq/mcp/server.py` with:

```python
"""Hail MCP server — remote app exposing both MCP transports.

The deployable artifact is ``app``. FastMCP serves Streamable HTTP at
``/mcp`` (the current MCP transport) and legacy SSE at ``/sse`` +
``/messages/`` during the transition window. We add ``/healthz`` to the
same Starlette app so the compose healthcheck stays a one-line probe
instead of spawning an MCP handshake per check.

Streamable HTTP needs its session manager running for the lifetime of
the app. ``FastMCP.streamable_http_app()`` wires that into its own
Starlette lifespan, but here we own the combined parent app, so we drive
``session_manager.run()`` from the parent lifespan ourselves.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from hailhq.mcp.hail_client import HailClient
from hailhq.mcp.tools import register_tools


def _build_app() -> tuple[FastMCP, HailClient, Starlette]:
    mcp_app: FastMCP = FastMCP(name="hail")
    client = HailClient()
    register_tools(mcp_app, client)

    # Build both transports. streamable_http_app() lazily creates the
    # session manager that the lifespan below runs; call it before the
    # lifespan references mcp_app.session_manager.
    sse_app = mcp_app.sse_app()  # /sse + /messages/
    http_app = mcp_app.streamable_http_app()  # /mcp

    async def healthz(_request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with mcp_app.session_manager.run():
            yield

    # NOTE: splatting sub_app.routes drops sub_app.user_middleware. Both
    # middleware lists are empty today (no auth configured). Phase 1c
    # (oauth-rs mode) configures a token verifier, which makes FastMCP add
    # AuthenticationMiddleware + AuthContextMiddleware inside sse_app() /
    # streamable_http_app() — that spec must either fold sub_app.user_middleware
    # in here or switch to a FastMCP-owned app with a different combining strategy.
    app = Starlette(
        routes=[
            Route("/healthz", healthz, methods=["GET"]),
            *sse_app.routes,
            *http_app.routes,
        ],
        lifespan=lifespan,
    )
    return mcp_app, client, app


mcp_app, hail_client, app = _build_app()


__all__ = ["app", "mcp_app", "hail_client"]
```

- [ ] **Step 4: Run the route test to verify it passes**

Run: `cd mcp && uv run pytest tests/test_server.py::test_app_exposes_both_transports_and_healthz -v`
Expected: PASS — `/healthz`, `/sse`, `/messages/`, and `/mcp` are all present.

- [ ] **Step 5: Add the lifespan startup test**

Append to `mcp/tests/test_server.py`:

```python
def test_healthz_ok_under_lifespan() -> None:
    # Entering TestClient as a context manager runs the app lifespan,
    # which drives StreamableHTTPSessionManager.run(). A clean startup +
    # 200 from /healthz proves the parent lifespan is wired correctly
    # (a misconfigured lifespan raises on context-manager enter).
    _mcp_app, _client, app = _build_app()
    with TestClient(app) as test_client:
        resp = test_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
```

- [ ] **Step 6: Run both new tests**

Run: `cd mcp && uv run pytest tests/test_server.py -v`
Expected: PASS (2 passed). If `test_healthz_ok_under_lifespan` errors on startup, the lifespan is misconfigured — re-check that `streamable_http_app()` is called in `_build_app` before the lifespan reads `mcp_app.session_manager`.

- [ ] **Step 7: Run the full suite to confirm no regression**

Run: `cd mcp && uv run pytest -v`
Expected: PASS — all existing `test_tools.py` tests (the five tools) still pass unchanged.

- [ ] **Step 8: Commit**

```bash
git add mcp/hailhq/mcp/server.py mcp/tests/test_server.py
git commit -m "feat(mcp): serve Streamable HTTP at /mcp alongside legacy SSE"
```

---

### Task 2: Document the Streamable HTTP endpoint

**Files:**

- Modify: `docs/setup/mcp.md`

- [ ] **Step 1: Re-read the current file**

Run: `sed -n '1,70p' docs/setup/mcp.md` (it was recently reformatted by prettier — match the live text, not this plan's memory of it).

- [ ] **Step 2: Replace the intro paragraph and the `## URL` section**

Change the opening line from describing SSE-only to both transports, and rewrite the URL section. New content for the top of the file (through the `## URL` block):

```markdown
# MCP clients

Hail exposes MCP as a **remote server**. The current transport is **Streamable HTTP** at `/mcp`; the legacy **SSE** endpoint at `/sse` stays mounted during a transition window. Agents connect by URL; no local install.

> Looking for the easy onboarding path? The [client picker on hail.so/mcp](https://hail.so/mcp) has copy-paste setup snippets for the 8 most common clients (Claude.ai, ChatGPT, Cursor, Gemini, …). This page is the technical reference behind those snippets.

## URL

- **Self-hosted**: `http://<your-host>:8081/mcp` (Streamable HTTP) — `/sse` still works for older clients.
- **Hail Cloud** (later): `https://mcp.hail.so/mcp`

Authenticate with your `HAIL_API_KEY` as a bearer token.
```

- [ ] **Step 3: Point the client config examples at `/mcp` with the HTTP transport**

In the `## Claude Code / Cursor` section, change the JSON example to use `"type": "http"` and the `/mcp` path:

```json
{
  "mcpServers": {
    "hail": {
      "type": "http",
      "url": "http://localhost:8081/mcp",
      "headers": {
        "Authorization": "Bearer ${HAIL_API_KEY}"
      }
    }
  }
}
```

and change the CLI line to:

```sh
claude mcp add --transport http hail http://localhost:8081/mcp \
  --header "Authorization: Bearer ${HAIL_API_KEY}"
```

In the `## Claude Desktop` section, change the `mcp-remote` URL argument from `http://localhost:8081/sse` to `http://localhost:8081/mcp` (mcp-remote negotiates Streamable HTTP automatically). Leave the rest of that section unchanged.

- [ ] **Step 4: Update the SSE-specific wording in the `## Why no stdio` section**

In that section, change the phrase "Every terminal client also accepts SSE." to "Every terminal client also accepts a remote URL." and adjust the surrounding sentence so it reads in terms of the remote HTTP endpoint rather than SSE specifically. Keep the four numbered reasons otherwise intact.

- [ ] **Step 5: Verify the doc reads correctly**

Run: `sed -n '1,70p' docs/setup/mcp.md`
Expected: intro mentions Streamable HTTP `/mcp` as primary and `/sse` as legacy; examples use `/mcp` + `type: http`; no remaining claim that the server is SSE-only.

- [ ] **Step 6: Commit**

```bash
git add docs/setup/mcp.md
git commit -m "docs(mcp): document /mcp Streamable HTTP endpoint, /sse as legacy"
```

---

## Deferred follow-ups (NOT part of this plan)

Gated on the new server being deployed and `/mcp` verified reachable in production:

- **`hail-website` client snippets** (`app/mcp/clients.ts`, `app/mcp/page.tsx`, `app/components/CodePanel.tsx`): migrate each card to the Streamable HTTP form (`type: http` / `--transport http` / VS Code `type: http`) pointing at `https://mcp.hail.so/mcp`. These currently target the (not-yet-live) cloud endpoint, so they are aspirational either way — migrate them as a focused follow-up to keep this plan single-repo and testable.
- **Retire `/sse`** after a transition window once telemetry shows no clients using it.
- **A _missing_ lifespan is not caught by this plan's tests.** `test_healthz_ok_under_lifespan` catches a _misconfigured_ lifespan (one that raises on enter), but if someone deletes `lifespan=lifespan`, `/healthz` still returns 200 while `/mcp` silently breaks at request time. Catching that needs a real MCP `initialize` handshake against `/mcp` — that belongs to the Phase 3 integration-test workstream.

## Self-Review

- **Spec coverage (Phase 0a):** "swap `sse_app()` → `streamable_http_app()`" → Task 1 (both are now built and combined); "keep `/sse` mounted during transition" → Task 1 (`*sse_app.routes`) + Task 2 wording; "keep `/healthz`" → Task 1 (`Route("/healthz", …)`) + test; "update `docs/setup/mcp.md`" → Task 2; "hail-website snippets once live" → Deferred section (justified). "No behavior change to the 5 tools" → Task 1 Step 7 runs the full existing suite.
- **Placeholder scan:** none — every code/doc step shows the exact content or the exact transformation.
- **Type/name consistency:** `_build_app()` returns `(mcp_app, client, app)` in both server.py and the tests; `app` is the module-level Starlette and the uvicorn target (`hailhq.mcp.server:app`, unchanged in the Dockerfile/compose). `_route_paths` is defined once and used in one test.
- **Risk guard:** the lifespan gotcha is called out up front and exercised by `test_healthz_ok_under_lifespan` (which catches a _misconfigured_ lifespan; the _missing_-lifespan case is noted in Deferred follow-ups for Phase 3).
- **Known no-op duplication:** both `sse_app()` and `streamable_http_app()` append `mcp_app._custom_starlette_routes` to their route lists, so splatting both would double-include any `@custom_route` routes. None exist today (harmless), but worth knowing if custom routes are added later.
