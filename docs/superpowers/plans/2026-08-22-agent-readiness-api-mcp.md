# API Versioning, Rate Limits, OpenAPI Descriptions, and MCP Public Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close four gaps an "Is Agentic" readiness audit found in the Hail API and MCP server: no URL versioning or deprecation signal, no general rate-limit headers, sparse OpenAPI operation/field descriptions, and an MCP server that requires OAuth even for `initialize`/`tools/list` (capability discovery), blocking agents from seeing what Hail offers before deciding to authenticate.

**Architecture:** All four are additive, backward-compatible changes to `api/` and `mcp/` — no existing authenticated behavior changes. Versioning dual-mounts every customer-facing router at `/v1/<resource>` (canonical, the only mount included in the OpenAPI schema) while keeping the unprefixed path alive at the routing layer (`include_in_schema=False`, so it does not duplicate operationIds or paths in the spec) with a `Deprecation` response header (the widely-deployed `Deprecation: true` form; RFC 9745 is the current authority for this header — RFC 8594 defines `Sunset`, a different header, not used here), so no existing integration breaks and the spec keeps a unique operationId per operation. Rate limiting is in-memory (`slowapi`/`limits`), keyed off the raw bearer token — this deployment is single-VM (confirmed: `docker-compose.yml` on one host, no replica orchestration), so in-memory state is safe; Redis is not needed. OpenAPI descriptions are added directly on route decorators and Pydantic `Field()` declarations — the spec is generated from the app (`app.openapi()`), so no separate spec-editing step. MCP discovery adds one new Starlette middleware, inserted before FastMCP's own auth middleware, that recognizes exactly two JSON-RPC methods (`initialize`, `tools/list`) and supplies a synthetic bearer for them only — every other method (`tools/call`, `resources/*`, etc.) is completely untouched and still requires a real bearer.

**Tech Stack:** Python 3, FastAPI (async), Pydantic v2, SQLAlchemy async, pytest + a real Postgres test container, ruff + black + mypy. MCP: `mcp` SDK (`FastMCP`), Starlette, `mcp.server.auth`.

**Design basis:** Live investigation of this repo on 2026-08-22 (not a pre-existing spec) — `api/hailhq/api/main.py:275-292` (router registration), `api/hailhq/api/routes/*.py` (12 customer-facing routers, each `APIRouter(prefix="/<resource>", tags=[...])`), `api/hailhq/api/routes/internal/*.py` (6 internal routers, `include_in_schema=False`, out of scope), `api/hailhq/api/deps.py:69-96` (`Principal`) and `:344+` (`get_current_principal`), `core/hailhq/core/agent_caps.py` (existing DB-backed per-org velocity cap — a _different_ mechanism from this plan's general rate limiter; do not conflate them), `mcp/hailhq/mcp/server.py` (`_build_app`, middleware wiring) and `mcp/hailhq/mcp/auth.py` (`PassThroughVerifier` — accepts any non-empty bearer string as "valid," real validation happens downstream at the API), `openapi/openapi.yaml` (4,491 lines, 53 operations, generated — confirmed via `docs/public/contributing.md:44-57` and CI `.github/workflows/openapi-check.yml:29-42`, which re-imports the app and diffs, not a live-server call).

## Global Constraints

- Branch `feat/agent-readiness-api-mcp` off `main`, already created in the repo root at `/Users/r/playground/hail` (not a worktree — no other in-progress work on `main` to isolate from, per the repo's own precedent in `docs/superpowers/plans/2026-08-07-console-speechmatics-byo.md`).
- Conventional Commits. **Never** a `Co-Authored-By` / AI-attribution trailer.
- Test: `cd /Users/r/playground/hail/api && uv run pytest -q` (and `cd core && uv run pytest -q` for `core/` changes). Lint/format before every commit: `uv run ruff check --fix .` then `uvx black .` (run from the relevant package directory — `api/` or `core/` or `mcp/`).
- **OpenAPI is source of truth for the CLI** (repo invariant, `CLAUDE.md`). After Tasks 1, 3, and 4 (anything that changes a route signature, description, or schema field), regenerate `openapi/openapi.yaml` in the same commit — see Task 6 for the exact regen command. Do not hand-edit the YAML.
- Do not touch the 6 `internal/*` routers (`api/hailhq/api/routes/internal/*.py`) in any task — they are `include_in_schema=False`, not part of the public/customer API surface this plan targets, and are called by internal services, not customer API keys.
- Do not touch `core/hailhq/core/agent_caps.py` or the agent self-signup velocity caps — that is a different, already-shipped mechanism (per-channel send-count ceiling for agent-origin orgs). This plan's rate limiter is a _general_ request-rate limiter across all customer-facing routes, unrelated to and additive with the existing caps.
- Every task that changes route behavior must not break an existing, unauthenticated-vs-authenticated distinction: unprefixed paths keep working exactly as today (Task 1), rate limiting must not reject any request that would have succeeded before this plan except by exceeding the new limit (Task 2), and MCP's `tools/call` and every method except `initialize`/`tools/list` must 401 exactly as before when unauthenticated (Task 5).
- No dependency additions without checking AGPLv3 license compatibility (repo invariant). `slowapi` (MIT) and its dependency `limits` (MIT) are both compatible — confirm this in Task 2 regardless, don't just trust this plan.

---

### Task 1: `/v1` URL versioning with backward-compatible dual-mount

**Files:**

- Modify: `api/hailhq/api/main.py` (router registration block, currently lines 275-292)
- Create: `api/hailhq/api/deprecation.py` (small middleware: stamps `Deprecation: true` + `Link: <https://api.hail.so/v1/...>; rel="successor-version"` on responses to unprefixed paths)
- Create: `docs/public/versioning.md` (documents what `/v1` means and how deprecation is signaled — the audit item asks this to be documented, not just header-only)
- Test: `api/tests/test_versioning.py`

**Interfaces:**

- Produces: every one of the 12 customer-facing routers becomes reachable at both `/v1/<resource>/...` (canonical, the only mount in the OpenAPI schema — one operationId per operation, unchanged from before this task) and `/<resource>/...` (legacy, still works, routable but `include_in_schema=False` so it does not appear in the spec, and now carries a `Deprecation` response header). No route handler code changes — same `APIRouter` objects, mounted twice.
- Consumes: the existing `router` objects exported by each of `calls.py`, `email_attachments.py`, `emails.py`, `events.py`, `email_domains.py`, `numbers.py`, `webhooks.py`, `unsubscribe.py`, `sms.py`, `contacts.py`, `whoami.py`, `providers.py` (all already imported in `main.py`).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_versioning.py
from __future__ import annotations

import uuid

import httpx
import pytest
from hailhq.core.models import ApiKey


async def test_v1_prefix_reaches_whoami(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get("/v1/whoami", headers={"Authorization": f"Bearer {plain_key}"})
    assert resp.status_code == 200


async def test_unprefixed_path_still_works_and_is_marked_deprecated(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get("/whoami", headers={"Authorization": f"Bearer {plain_key}"})
    assert resp.status_code == 200
    assert resp.headers["deprecation"] == "true"
    assert 'rel="successor-version"' in resp.headers["link"]
    assert "/v1/whoami" in resp.headers["link"]


async def test_v1_path_is_not_marked_deprecated(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get("/v1/whoami", headers={"Authorization": f"Bearer {plain_key}"})
    assert "deprecation" not in resp.headers


def test_legacy_unprefixed_paths_are_not_in_the_openapi_schema() -> None:
    from hailhq.api.main import app

    schema = app.openapi()
    paths = schema["paths"]
    assert "/v1/whoami" in paths
    assert "/whoami" not in paths


async def test_internal_routes_are_not_dual_mounted(client: httpx.AsyncClient) -> None:
    # /internal/... must not also exist at /v1/internal/... — internal routers
    # were never versioned; a bare-string prefix match on "/v1" + internal's
    # own "/internal" prefix would be a real path if this task's loop is too
    # broad. Confirm it 404s. Adjust the exact sub-path to a real internal
    # route if "ses-events/healthz" doesn't exist verbatim — check
    # api/hailhq/api/routes/internal/ses_events.py for its real paths first.
    resp = await client.get("/v1/internal/ses-events/healthz")
    assert resp.status_code == 404
```

This uses the real `client` and `org_and_key` fixtures already defined in `api/tests/conftest.py:256-283` (a `httpx.AsyncClient` wired to the real `app` with common provider mocks overridden) and `:284-287` (returns `(organization_id, ApiKey, plain_key_string)`) — the same pattern `api/tests/test_auth_shared.py` and the rest of the suite already use for real-app integration tests against a live-shaped API key. No `@pytest.mark.asyncio`/`anyio` decorator is needed on async test functions in `api/` — confirmed `api/pyproject.toml:47` sets `asyncio_mode = "auto"`, so a bare `async def test_...` is enough (this differs from `mcp/`, which uses `@pytest.mark.asyncio` explicitly — see Task 5 — don't copy that convention into `api/` tests or vice versa).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_versioning.py -v`
Expected: FAIL — `/v1/whoami` 404s (no such route yet), and the unprefixed path has no `Deprecation` header.

- [ ] **Step 3: Implement the dual-mount**

In `api/hailhq/api/main.py`, replace the current block:

```python
app.include_router(calls_routes.router)
app.include_router(email_attachments_routes.router)
app.include_router(emails_routes.router)
app.include_router(events_routes.router)
app.include_router(email_domains_routes.router)
app.include_router(numbers_routes.router)
app.include_router(webhooks_routes.router)
app.include_router(unsubscribe_routes.router)
app.include_router(sms_routes.router)
app.include_router(contacts_routes.router)
app.include_router(whoami_routes.router)
app.include_router(providers_routes.router)
```

with:

```python
# Customer-facing routers are dual-mounted: /v1/<resource> is canonical
# and the only mount that appears in the OpenAPI schema. The unprefixed
# path keeps working for existing integrations (routable, real handler)
# but is excluded from the schema (include_in_schema=False) so it does
# not create a second, colliding operationId per operation — FastAPI
# derives operationId from the function name, and a route mounted twice
# with schema inclusion on both would produce duplicate IDs and a spec
# with two paths per operation. The unprefixed path is marked
# Deprecation: true instead (see deprecation.py). No route handler is
# duplicated, both mounts point at the same router object.
_CUSTOMER_ROUTERS = [
    calls_routes.router,
    email_attachments_routes.router,
    emails_routes.router,
    events_routes.router,
    email_domains_routes.router,
    numbers_routes.router,
    webhooks_routes.router,
    unsubscribe_routes.router,
    sms_routes.router,
    contacts_routes.router,
    whoami_routes.router,
    providers_routes.router,
]
for _router in _CUSTOMER_ROUTERS:
    app.include_router(_router, prefix="/v1")
    app.include_router(_router, include_in_schema=False)
```

Then add the deprecation-stamping middleware. Create `api/hailhq/api/deprecation.py`:

```python
"""Marks legacy (unprefixed) customer-API responses as deprecated.

/v1/<resource> is canonical (see main.py's router dual-mount). The
unprefixed path keeps working — no existing integration breaks — but
every response on it carries a Deprecation: true header (the widely
deployed form; RFC 9745 is the current authority for this header) plus a
Link pointing at the /v1 successor, so a client (or an agent reading the
response) can tell the path is being phased out without guessing.

Matches on path shape, not a route allowlist: any request whose path does
NOT start with /v1/ and does NOT start with /internal/ is presumed to be
hitting a legacy-mounted customer route. /healthz and unmatched 404s also
pass through this middleware harmlessly (a Deprecation header on a 404 or
a healthcheck is inert, not incorrect) — this stays simple rather than
duplicating the router list here too.
"""

from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

_V1_PREFIX = "/v1/"
_INTERNAL_PREFIX = "/internal/"


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if not path.startswith(_V1_PREFIX) and not path.startswith(_INTERNAL_PREFIX):
            response.headers["Deprecation"] = "true"
            versioned = "/v1" + path
            response.headers["Link"] = f'<{versioned}>; rel="successor-version"'
        return response
```

Register it in `main.py` right after `app = FastAPI(...)`:

```python
from hailhq.api.deprecation import DeprecationHeaderMiddleware
# ...
app.add_middleware(DeprecationHeaderMiddleware)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_versioning.py -v`
Expected: PASS (all 5 cases)

- [ ] **Step 5: Run the full API test suite to confirm no regression**

Run: `cd api && uv run pytest -q`
Expected: all pass. Every existing test that calls an unprefixed path (the whole existing suite, since nothing used `/v1` before this task) must still pass unchanged — that's the point of the dual-mount.

- [ ] **Step 6: Write a short versioning doc**

The audit item behind this task asks for more than the header alone — it asks that how deprecation is signaled be documented somewhere an agent (or a human integrator) can read it, not just infer from response headers. Create `docs/public/versioning.md`:

```markdown
# API versioning

`/v1/<resource>` is the canonical, documented form of every customer-facing
Hail API route. It appears in the OpenAPI spec (`openapi/openapi.yaml`) and
is what the CLI and generated clients target.

## Legacy unprefixed paths

Routes without the `/v1` prefix (e.g. `/whoami` instead of `/v1/whoami`)
still work, for existing integrations built before versioning shipped. They
are not in the OpenAPI spec and should not be used for new integrations.

Every response from a legacy path carries:

- `Deprecation: true` — this path is deprecated (see the IETF Deprecation
  HTTP header field).
- `Link: <https://api.hail.so/v1/...>; rel="successor-version"` — the
  canonical `/v1` path that replaces it.

## Sunset

No sunset date is set for the legacy paths yet. If one is scheduled, it
will be announced here and via a `Sunset` response header (RFC 8594) added
ahead of the change, giving integrators advance notice before the
unprefixed paths stop working.
```

- [ ] **Step 7: Lint and format**

Run: `cd api && uv run ruff check --fix . && uvx black .`

- [ ] **Step 8: Commit**

```bash
git add hailhq/api/main.py hailhq/api/deprecation.py tests/test_versioning.py ../docs/public/versioning.md
git commit -m "feat(api): dual-mount every customer router at /v1, deprecate unprefixed paths"
```

(Regenerating `openapi/openapi.yaml` happens once, in Task 6, after Tasks 1/3/4 are all in — regenerating after every task would just produce three overlapping diffs of the same file.)

---

### Task 2: General rate-limit headers

**Files:**

- Modify: `api/pyproject.toml` (add `slowapi` dependency)
- Create: `api/hailhq/api/ratelimit.py`
- Modify: `api/hailhq/api/main.py` (wire the limiter)
- Modify: `core/hailhq/core/config.py` (one new setting: default requests-per-minute ceiling)
- Test: `api/tests/test_ratelimit.py`

**Interfaces:**

- Produces: every response from a customer-facing route (both `/v1/...` and legacy unprefixed, per Task 1's dual-mount — the limiter is keyed on the raw bearer, which is identical on both mounts of the same call) carries `RateLimit-Limit`, `RateLimit-Remaining`, `RateLimit-Reset` (IETF `draft-ietf-httpapi-ratelimit-headers` conventions — the header names, not a specific RFC number, since that draft is not yet an RFC; if `slowapi`'s header names differ from these three, use `slowapi`'s actual header names and note the difference in your report rather than fighting the library). A 429 additionally carries `Retry-After`.
- Consumes: nothing from Task 1 directly — independent of the versioning dual-mount, applies identically to both mounts since it's global middleware keyed on the request, not the matched route.

- [ ] **Step 1: Confirm the dependency and its license**

Run: `cd api && uv add slowapi` (this also pulls in `limits`, its underlying rate-tracking engine). Then confirm both `slowapi` and `limits` are MIT-licensed (check their PyPI project pages or `uv run pip show slowapi limits` for the `License` field) — do not proceed past this step if either turns out to be non-permissive; report back instead.

- [ ] **Step 2: Add the setting**

In `core/hailhq/core/config.py`, add alongside the existing `agent_*` cap settings (same file, same style — read the surrounding `Settings` class fields first to match the naming/env-var convention exactly):

```python
    api_rate_limit_per_minute: int = 300
```

(300/min = 5 req/sec sustained per caller — generous for legitimate agent/automation traffic, still bounds a runaway loop. This number is a starting point, not a researched-and-final threshold; flag it clearly as tunable in your commit message, and if `Settings` has an existing doc-comment convention for tunable values, follow it here too.)

- [ ] **Step 3: Write the failing test**

```python
# api/tests/test_ratelimit.py
from __future__ import annotations

import uuid

import httpx
import pytest
from hailhq.core.models import ApiKey
from hailhq.core.config import settings


async def test_response_carries_ratelimit_headers(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get("/v1/whoami", headers={"Authorization": f"Bearer {plain_key}"})
    assert resp.status_code == 200
    assert "ratelimit-limit" in resp.headers or "x-ratelimit-limit" in resp.headers


async def test_exceeding_the_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, plain_key = org_and_key
    # Drop the ceiling to something trivially exceedable within one test,
    # rather than firing 300+ real requests.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    headers = {"Authorization": f"Bearer {plain_key}"}
    for _ in range(2):
        resp = await client.get("/v1/whoami", headers=headers)
        assert resp.status_code == 200
    resp = await client.get("/v1/whoami", headers=headers)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


async def test_internal_routes_are_not_rate_limited(client: httpx.AsyncClient) -> None:
    # Internal routes are called by internal services on a trusted path,
    # not customer API keys — confirm the limiter's key_func (bearer-based)
    # doesn't apply to a route with no Authorization header at all, and that
    # this doesn't crash the key function.
    ...  # implement against whichever internal route is easiest to call
         # unauthenticated in the existing internal test suite's style —
         # check api/tests/ for how internal routes are already tested
         # (search for "internal" in api/tests/*.py for the real pattern).
```

Note: `monkeypatch.setattr(settings, ...)` only works if `slowapi`'s limiter reads the ceiling dynamically per-request rather than capturing it once at app-startup/import time. If your Step 4 implementation captures the limit as a static decorator argument (the more common `slowapi` pattern, e.g. `@limiter.limit("300/minute")`), this specific test's monkeypatch approach won't work — adapt the test to either use a dependency-override pattern (check `api/tests/conftest.py` for how other settings-dependent tests already do this in this repo) or parametrize the limiter's key/limit per-test some other way. Get the real mechanism working before insisting on the monkeypatch approach as written here.

- [ ] **Step 4: Implement the limiter**

Create `api/hailhq/api/ratelimit.py`:

```python
"""General per-caller request-rate limiting.

Distinct from core.agent_caps (a DB-backed, per-org, per-channel *send*
velocity cap for agent-origin orgs only). This is a general HTTP
request-rate limiter across every customer-facing route, for every
caller. In-memory storage — safe because this API runs single-instance
(one VM, docker-compose, no replica orchestration; see deploy.yml). If a
second instance is ever added, swap slowapi's storage_uri to Redis; no
call-site changes needed.

Keyed on the raw Authorization header value (hashed) rather than the
resolved Principal, so the limiter runs before any DB round-trip —
distinct callers with distinct keys/tokens get distinct buckets even
before auth resolves whether the token is valid.
"""

from __future__ import annotations

import hashlib

from slowapi import Limiter
from slowapi.util import get_remote_address
from starlette.requests import Request

from hailhq.core.config import settings


def _rate_limit_key(request: Request) -> str:
    auth = request.headers.get("authorization")
    if not auth:
        return get_remote_address(request)
    return hashlib.sha256(auth.encode()).hexdigest()


limiter = Limiter(key_func=_rate_limit_key)


def rate_limit_string() -> str:
    return f"{settings.api_rate_limit_per_minute}/minute"
```

Wire it in `main.py` (check `slowapi`'s current recommended FastAPI integration — the app-level `app.state.limiter = limiter` + `app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)` + `SlowAPIMiddleware` pattern is standard as of `slowapi` 0.1.x, but confirm against the version `uv add` actually installed rather than assuming). Apply the limit to the 12 customer routers only (skip `internal_*` and `/healthz`) — check whether `slowapi` supports a middleware-level default limit with a per-route exemption list, or whether it's cleaner to add the limiter as a dependency on each customer router's `APIRouter(...)` construction (`dependencies=[Depends(limiter.limit(...))]` or similar — the exact mechanism depends on the installed `slowapi` version's API, which you'll confirm in Step 1).

- [ ] **Step 5: Document the new 429 in OpenAPI, following the codebase's existing convention exactly**

This codebase already has a precedent for exactly this: `api/hailhq/api/agent_gate.py:22-33` defines `RATE_LIMITED_RESPONSES` (a `dict[int | str, dict[str, Any]]` documenting the agent-abuse-cap 429's shape, including a `Retry-After` header), and `calls.py`/`sms.py`/`emails.py`'s create-route decorators pass it as `responses=RATE_LIMITED_RESPONSES`. The file's own top comment says why: "FastAPI does not infer statuses from `raise HTTPException`... the create routes must declare this on their decorator... for the generated spec — and the CLI codegen from it — to reflect the rate limit." Your new general limiter's 429 needs the same treatment, or an agent reading the OpenAPI spec has no way to know a 429 is even possible on 51 of the 53 operations that don't already have it.

Add a sibling constant in `api/hailhq/api/ratelimit.py` (same file as the limiter, since it documents that limiter's own error shape):

```python
from typing import Any

GENERAL_RATE_LIMITED_RESPONSES: dict[int | str, dict[str, Any]] = {
    429: {
        "description": (
            "Rate limited. This caller exceeded the general request-rate "
            "ceiling. Retry after the Retry-After header (seconds)."
        ),
        "headers": {
            "Retry-After": {
                "description": "Seconds to wait before retrying.",
                "schema": {"type": "integer"},
            },
            "RateLimit-Limit": {
                "description": "The request ceiling for the current window.",
                "schema": {"type": "integer"},
            },
            "RateLimit-Remaining": {
                "description": "Requests remaining in the current window.",
                "schema": {"type": "integer"},
            },
            "RateLimit-Reset": {
                "description": "Seconds until the current window resets.",
                "schema": {"type": "integer"},
            },
        },
    }
}
```

(Adjust the header names inside this dict to match whatever `slowapi` actually emits, confirmed in Step 4 — don't document header names the implementation doesn't actually send.)

Then add `responses=GENERAL_RATE_LIMITED_RESPONSES` to every one of the 53 route decorators across the 12 customer route files. On the 3 routes that already carry `responses=RATE_LIMITED_RESPONSES` (`calls.py`'s create route, `sms.py`'s create route, `emails.py`'s create route), **merge the two dicts' `429` keys** rather than passing one that clobbers the other — both 429 reasons are real and distinct on those specific routes (the general limiter can fire before the agent-abuse gate is ever reached, and vice versa isn't true, but both are genuinely possible responses a caller needs documented). A simple `{**RATE_LIMITED_RESPONSES, 429: {**RATE_LIMITED_RESPONSES[429], "description": RATE_LIMITED_RESPONSES[429]["description"] + " " + GENERAL_RATE_LIMITED_RESPONSES[429]["description"], "headers": {**RATE_LIMITED_RESPONSES[429]["headers"], **GENERAL_RATE_LIMITED_RESPONSES[429]["headers"]}}}` (or a small named helper, your call on the cleanest shape) captures both without losing either.

This step touches the same 12 files Task 3 touches (route decorators). If Task 3 hasn't run yet when you do this, no conflict — you're each adding a different kwarg/docstring to the same decorators, not fighting over the same lines. If Task 3 already ran, just add `responses=` alongside the description that's already there.

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_ratelimit.py -v`
Expected: PASS

- [ ] **Step 7: Run the full suite**

Run: `cd api && uv run pytest -q`
Expected: all pass — in particular, confirm no existing test fires 300+ requests against the same route in one test run and now trips the new limiter (grep the existing test suite for any loop calling a customer route many times before assuming this is clean).

- [ ] **Step 8: Lint and format**

Run: `cd api && uv run ruff check --fix . && uvx black .`

- [ ] **Step 9: Commit**

```bash
git add hailhq/api/ratelimit.py hailhq/api/main.py hailhq/api/routes/ pyproject.toml uv.lock ../core/hailhq/core/config.py tests/test_ratelimit.py
git commit -m "feat(api): general in-memory rate limiting with RateLimit-*/Retry-After headers"
```

---

### Task 3: OpenAPI operation descriptions

**Files:**

- Modify: all 12 files under `api/hailhq/api/routes/` that register a customer-facing route (`calls.py`, `email_attachments.py`, `emails.py`, `events.py`, `email_domains.py`, `numbers.py`, `webhooks.py`, `unsubscribe.py`, `sms.py`, `contacts.py`, `whoami.py`, `providers.py`)
- Test: `api/tests/test_openapi_descriptions.py`

**Interfaces:**

- Produces: every one of the 53 operations gets a real `description` (via the route decorator's `description=` kwarg, or a docstring on the handler function — FastAPI uses the function docstring as the description when no explicit `description=` is given, so either mechanism satisfies this task; pick whichever a given file already leans toward, or default to a docstring since it doubles as in-code documentation for a future reader).
- Consumes: nothing from Tasks 1/2.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_openapi_descriptions.py
from hailhq.api.main import app


def test_every_public_operation_has_a_real_description() -> None:
    schema = app.openapi()
    missing = []
    for path, methods in schema["paths"].items():
        if path.startswith("/internal") or path.startswith("/v1/internal"):
            continue
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            desc = (op.get("description") or "").strip()
            if len(desc) < 20:
                missing.append(f"{method.upper()} {path}")
    assert not missing, f"{len(missing)} operations still lack a real description: {missing}"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd api && uv run pytest tests/test_openapi_descriptions.py -v`
Expected: FAIL, listing most of the 53 operations. Task 1's legacy mount is `include_in_schema=False`, so the schema has exactly one entry per operation (at `/v1/...`), not two — the failure count is 53, not 106.

- [ ] **Step 3: Write descriptions**

For each route handler in the 12 files, add a docstring (or `description=` kwarg) that states: what the operation does, in plain language a caller (human or agent) can act on without reading the handler body; any non-obvious side effect (e.g. "triggers an outbound call within seconds," "queues async delivery — use the webhook or GET this resource to confirm final status," "irreversible — the number is permanently released"); and any consent/compliance precondition the caller must already have satisfied (several routes require `recipient_consent` on the request body per `core/hailhq/core/schemas.py` — check for that field's presence in the operation's request model and mention the requirement when it's there, e.g. calls/sms/emails).

Example — `api/hailhq/api/routes/calls.py`'s `create_call` (currently no docstring, currently the empty `description: ""` this task exists to fix):

```python
@router.post("", response_model=CallResponse, status_code=201)
async def create_call(...):
    """Place an outbound AI voice call.

    The call is placed asynchronously — this returns as soon as the call is
    queued, not when it completes. Poll GET /calls/{call_id} or configure a
    webhook to get the final status and transcript. Requires
    recipient_consent=true on the request body; Hail does not verify lawful
    basis to contact the recipient, the caller warrants it.
    """
```

Do this for all 53 operations across the 12 files. Where a file already has some docstrings and some blank ones (check each file individually — don't assume uniform blankness), only touch the blank ones; don't rewrite an existing adequate description just to match this example's exact voice.

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd api && uv run pytest tests/test_openapi_descriptions.py -v`
Expected: PASS

- [ ] **Step 5: Run the full suite**

Run: `cd api && uv run pytest -q`
Expected: all pass (docstring-only changes, but confirm nothing else broke).

- [ ] **Step 6: Lint and format**

Run: `cd api && uv run ruff check --fix . && uvx black .`

- [ ] **Step 7: Commit**

```bash
git add hailhq/api/routes/
git commit -m "docs(api): add real descriptions to all 53 public operations"
```

---

### Task 4: OpenAPI schema field descriptions

**Files:**

- Modify: `core/hailhq/core/schemas.py`
- Test: `api/tests/test_openapi_descriptions.py` (extend the file from Task 3 with a field-level check)

**Interfaces:**

- Produces: every field on a request or response Pydantic model reachable from a public operation gets a `Field(description=...)`.
- Consumes: nothing from Tasks 1-3, but naturally sequenced after Task 3 since both feed the same regeneration step in Task 6.

- [ ] **Step 1: Extend the failing test**

Add to `api/tests/test_openapi_descriptions.py`. This is real, confirmed-working code — verified against a live `app.openapi()` dump on 2026-08-22 (`POST /calls`'s `requestBody.content["application/json"].schema.$ref == "#/components/schemas/CallCreate"`; `CallListResponse.properties.items == {"items": {"$ref": "..."}, "type": "array", ...}`; nullable fields use `anyOf: [{...}, {"type": "null"}]`, confirmed on `CallListResponse.properties.next_cursor`):

```python
def _collect_schema_refs(node: object, found: set[str]) -> None:
    """Recursively collect every #/components/schemas/<Name> reference
    reachable from `node` — handles direct $ref, array `items`, and the
    `anyOf` shape Pydantic v2 uses for `X | None` fields."""
    if isinstance(node, dict):
        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/components/schemas/"):
            found.add(ref.removeprefix("#/components/schemas/"))
        for key in ("items", "requestBody", "schema"):
            if key in node:
                _collect_schema_refs(node[key], found)
        for key in ("anyOf", "oneOf", "allOf"):
            for sub in node.get(key, []):
                _collect_schema_refs(sub, found)
        content = node.get("content")
        if isinstance(content, dict):
            for media in content.values():
                _collect_schema_refs(media, found)
    elif isinstance(node, list):
        for item in node:
            _collect_schema_refs(item, found)


def test_every_public_schema_field_has_a_description() -> None:
    schema = app.openapi()
    referenced_schemas: set[str] = set()
    for path, methods in schema["paths"].items():
        if path.startswith("/internal") or path.startswith("/v1/internal"):
            continue
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if "requestBody" in op:
                _collect_schema_refs(op["requestBody"], referenced_schemas)
            for resp in op.get("responses", {}).values():
                _collect_schema_refs(resp, referenced_schemas)

    # Referenced schemas can themselves reference further schemas (e.g. a
    # response wraps a list of another model) — expand transitively until
    # the set stops growing.
    frontier = set(referenced_schemas)
    while frontier:
        new_refs: set[str] = set()
        for name in frontier:
            model = schema["components"]["schemas"].get(name, {})
            _collect_schema_refs(model, new_refs)
        frontier = new_refs - referenced_schemas
        referenced_schemas |= new_refs

    missing = []
    for name in sorted(referenced_schemas):
        model = schema["components"]["schemas"].get(name, {})
        for field_name, field_schema in model.get("properties", {}).items():
            if not field_schema.get("description"):
                missing.append(f"{name}.{field_name}")
    assert not missing, f"{len(missing)} public schema fields still lack a description: {missing}"
```

Run this against the real repo before treating it as final — the shapes above were confirmed on 2026-08-22 against this exact codebase, but re-verify with a fresh `app.openapi()` dump if anything about the schema generation has changed since (a FastAPI/Pydantic version bump, for instance, could shift the `anyOf`-for-nullable convention).

- [ ] **Step 2: Run to see it fail**

Run: `cd api && uv run pytest tests/test_openapi_descriptions.py::test_every_public_schema_field_has_a_description -v`
Expected: FAIL. Confirmed count against the live repo on 2026-08-22: 229 fields across 44 referenced schemas (e.g. `CallCreate.to`, `CallCreate.system_prompt`, `CallListResponse.items`, `Body_upload_email_attachment.file`) — re-confirm the exact count and list at implementation time in case the schema has changed since.

- [ ] **Step 3: Add field descriptions**

In `core/hailhq/core/schemas.py`, add `description=` to every `Field(...)` (or convert a bare type annotation to `Field(description=...)` where none exists yet) on every model reachable from a public operation's request or response. Prioritize by what an agent integrating blind actually needs to know: enum-valued fields (state which values mean what), fields with format constraints (E.164 phone numbers, ISO 8601 timestamps — say so), fields whose name alone is ambiguous (`state` on a Call could mean call-progress or US-state — many are like this, check), and any field whose absence vs. `null` vs. empty-string carries different meaning. For fields that are genuinely self-explanatory from their name and type alone (e.g. `id: UUID`), a short description is still expected (`"Unique identifier for this call."`) — brevity is fine, absence is not.

- [ ] **Step 4: Run to verify it passes**

Run: `cd api && uv run pytest tests/test_openapi_descriptions.py -v`
Expected: PASS (both tests from Task 3 and this task)

- [ ] **Step 5: Run the full suite**

Run: `cd core && uv run pytest -q && cd ../api && uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Lint and format**

Run: `cd core && uv run ruff check --fix . && uvx black .`

- [ ] **Step 7: Commit**

```bash
git add hailhq/core/schemas.py ../api/tests/test_openapi_descriptions.py
git commit -m "docs(core): add descriptions to every public API schema field"
```

---

### Task 5: MCP unauthenticated capability discovery (`initialize` + `tools/list`)

**Files:**

- Create: `mcp/hailhq/mcp/discovery_auth.py`
- Modify: `mcp/hailhq/mcp/server.py` (insert the new middleware first in the stack)
- Test: `mcp/tests/test_discovery_auth.py`

**Interfaces:**

- Produces: an unauthenticated `initialize` or `tools/list` JSON-RPC request to `mcp.hail.so` (oauth-rs mode) succeeds instead of 401ing. Every other method — `tools/call`, `resources/*`, `notifications/*`, `ping` — is completely unaffected: still 401s without a bearer, exactly as `test_oauth_rs_unauth_returns_401_with_resource_metadata` (`mcp/tests/test_server_transport.py:38-47`, pre-existing, must still pass unmodified) already asserts for `ping`.
- Consumes: `mcp.hailhq.mcp.auth.PassThroughVerifier` (unmodified) — the new middleware works by injecting a synthetic non-empty bearer for the two safelisted methods, which the _existing_ verifier already accepts (it accepts any non-empty token string as "valid," per its own docstring — deliberately, since real validation happens downstream at the API and these two methods never reach a downstream API call anyway).

- [ ] **Step 1: Write the failing tests**

Read `mcp/tests/test_server_transport.py` in full first — this task adds to the same file, reusing its `_boot(monkeypatch, oauth=True)` helper (reloads `hailhq.mcp.server` under oauth-rs env, returns the reloaded module — `srv.app` is the real Starlette app) and its `httpx.ASGITransport(app=srv.app)` + `httpx.AsyncClient(transport=transport, base_url="http://t")` pattern, exactly as `test_oauth_rs_unauth_returns_401_with_resource_metadata` (lines 38-47) already does. Use `@pytest.mark.asyncio` on each test — confirmed via that existing test, this package's convention (unlike `api/`, which has `asyncio_mode = "auto"` and needs no decorator — see Task 1's note).

**Session-handshake caveat, resolve before writing the real test:** MCP's Streamable HTTP transport is session-oriented — a real client normally calls `initialize` first, the server returns an `Mcp-Session-Id` response header, and subsequent calls (`tools/list`, `tools/call`, ...) include that header. The pre-existing `ping` test sends a bare `{"method": "ping"}` with no prior `initialize` and no session header and gets 401 — which only proves the _auth_ check runs before any session-state check, not that `tools/list` would work session-less once auth passes. Before asserting `tools/list` returns 200 with no session header, run it against a locally booted oauth-rs server (Step 6 gives you the exact `curl` commands) and see what actually comes back. If FastMCP requires a session, the test (and the manual curl in Step 6) needs two calls: `initialize` first (still with the synthetic-bearer path from this task, so it also succeeds unauthenticated), capture the `Mcp-Session-Id` response header, then send `tools/list` with that header attached. Write the test to match whatever the real protocol requires — don't assert a bare-`tools/list`-succeeds shape if the real server needs the session handshake first.

**Two more transport details to resolve against the real server, not guess:**

1. **`Accept` header.** The `mcp` SDK's Streamable HTTP transport requires the client to send `Accept: application/json, text/event-stream` on POST requests — a request without it (or with only one of the two) gets rejected with 406 before your new middleware's method-check even matters once auth passes. `httpx`'s `json=` kwarg sets `Accept: */*`, which will not satisfy a literal-substring check if the SDK does one. Send the header explicitly on every POST in the new tests: `headers={"Accept": "application/json, text/event-stream"}` (merge with the existing `Content-Type: application/json` header httpx sets automatically for `json=`).
2. **Response framing.** With FastMCP's default `json_response=False`, a successful POST response comes back as `Content-Type: text/event-stream`, body framed as SSE (`data: {...}` lines), not a bare JSON body — `resp.json()` will raise on a real success response. Before asserting on `body["result"]["tools"]`, check `resp.headers["content-type"]`: if it's `application/json`, `resp.json()` works as written below; if it's `text/event-stream`, parse the JSON out of the `data:` line(s) instead (e.g. `json.loads(next(l[len("data:"):].strip() for l in resp.text.splitlines() if l.startswith("data:")))`). Write the test to match whichever framing the real server actually returns — don't assume `resp.json()` works untested.

```python
# Add to mcp/tests/test_server_transport.py, near the existing
# test_oauth_rs_unauth_returns_401_with_resource_metadata test.
# This is the no-session-required shape; adapt per the caveat above if the
# real server needs an initialize -> Mcp-Session-Id -> tools/list handshake.

_MCP_ACCEPT = "application/json, text/event-stream"


def _parse_mcp_body(resp: httpx.Response) -> dict:
    """A success response may be framed as bare JSON or as SSE, depending
    on FastMCP's json_response setting — confirmed by inspecting the real
    server's response (see Step 6); this helper handles either."""
    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        return resp.json()
    data_line = next(
        line for line in resp.text.splitlines() if line.startswith("data:")
    )
    return json.loads(data_line[len("data:") :].strip())


@pytest.mark.asyncio
async def test_oauth_rs_unauth_initialize_succeeds(monkeypatch):
    """Capability discovery must not require a bearer."""
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/",
            headers={"Accept": _MCP_ACCEPT},
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "test", "version": "0.0.0"},
                },
            },
        )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_oauth_rs_unauth_tools_list_succeeds(monkeypatch):
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/",
            headers={"Accept": _MCP_ACCEPT},
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
    assert resp.status_code == 200
    body = _parse_mcp_body(resp)
    tool_names = {t["name"] for t in body["result"]["tools"]}
    assert "place_call" in tool_names
    assert "whoami" in tool_names


@pytest.mark.asyncio
async def test_oauth_rs_unauth_tools_call_still_401s(monkeypatch):
    """The safelist is exactly two methods — tools/call must still require auth."""
    srv = _boot(monkeypatch, oauth=True)
    transport = httpx.ASGITransport(app=srv.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.post(
            "/",
            headers={"Accept": _MCP_ACCEPT},
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "whoami", "arguments": {}},
            },
        )
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers
```

Add `import json` to the test file's imports if not already present (check `mcp/tests/test_server_transport.py`'s existing imports first — don't duplicate).

The pre-existing `test_oauth_rs_unauth_returns_401_with_resource_metadata` already covers `ping` staying 401 unauthenticated — do not duplicate it, just confirm it still passes unmodified in Step 4.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd mcp && uv run pytest tests/test_server_transport.py -k discovery_auth -v` (or however the new tests are named/selected)
Expected: FAIL — `initialize` and `tools/list` currently 401 same as everything else.

- [ ] **Step 3: Implement the middleware**

Create `mcp/hailhq/mcp/discovery_auth.py`:

```python
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
_SYNTHETIC_BEARER = b"authorization: bearer anonymous-discovery"


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

        has_auth = any(k.lower() == b"authorization" for k, _ in scope.get("headers", []))
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
            if messages:
                return messages.pop(0)
            return {"type": "http.disconnect"}

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
```

Wire it into `mcp/hailhq/mcp/server.py`'s `_build_app`, wrapping the final `app`. Starlette's `Starlette(middleware=[A, B, ...])` runs `A` outermost (first-listed = outermost = runs first on the way in) — so `DiscoveryAuthMiddleware` must be first in the list, ahead of FastMCP's own middleware, so it can inject the synthetic bearer before FastMCP's `AuthenticationMiddleware` reads the headers:

```python
from hailhq.mcp.discovery_auth import DiscoveryAuthMiddleware
from starlette.middleware import Middleware
# ...
middleware=[Middleware(DiscoveryAuthMiddleware), *http_app.user_middleware]
```

Confirm with a quick read of `server.py`'s existing `middleware=[...]` construction site (the comment there already documents that the FastMCP auth middleware must fire "before any route resolution" — this new entry goes before that one in the list, per the ordering above) rather than guessing the variable name.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd mcp && uv run pytest tests/test_server_transport.py -v`
Expected: PASS — including the pre-existing `test_oauth_rs_unauth_returns_401_with_resource_metadata`, unmodified and still green.

- [ ] **Step 5: Run the full MCP test suite**

Run: `cd mcp && uv run pytest -q`
Expected: all pass.

- [ ] **Step 6: Manual verification against a locally running server**

Run: `cd mcp && uv run uvicorn hailhq.mcp.server:app --reload --port 8081` (requires `HAIL_AUTH_URL` set per the dev-commands section of `CLAUDE.md` — check `.env.example` for what oauth-rs mode needs locally, or use whatever this repo's existing local-dev docs say for standing up oauth-rs mode specifically, since static-key mode wouldn't exercise this code path at all).

```bash
curl -s -i -X POST http://localhost:8081/ \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
# expect: 200, no WWW-Authenticate challenge. -i prints response headers —
# check Content-Type here: application/json means the body is bare JSON,
# text/event-stream means it's framed as `data: {...}` lines. Note which
# one it is and use that to finalize _parse_mcp_body in Step 1's tests.
# If the response also carries Mcp-Session-Id, capture it and pass it as
# -H "Mcp-Session-Id: <value>" on the tools/list call below.

curl -s -i -X POST http://localhost:8081/ \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
# expect: 200, a tools array including place_call, send_sms, send_email, whoami, etc.
# (in the JSON body directly, or inside a `data:` line — see above)

curl -s -o /dev/null -w "%{http_code}\n" -X POST http://localhost:8081/ \
  -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"whoami","arguments":{}}}'
# expect: 401 — unchanged from before this task
```

Record the literal output in your report — this is real evidence a unit test alone doesn't fully provide (confirms the ASGI-level header injection actually works against a running server, not just the test client).

- [ ] **Step 7: Lint and format**

Run: `cd mcp && uv run ruff check --fix . && uvx black .`

- [ ] **Step 8: Commit**

```bash
git add hailhq/mcp/discovery_auth.py hailhq/mcp/server.py tests/test_server_transport.py
git commit -m "feat(mcp): allow unauthenticated initialize + tools/list for capability discovery"
```

---

### Task 6: Regenerate `openapi/openapi.yaml` and verify CI's diff check

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated, not hand-edited)

**Interfaces:**

- Consumes: the combined effect of Tasks 1, 2, 3, and 4 (new `/v1/...` paths, the new general-limiter 429 response shape, new operation descriptions, new field descriptions — confirmed via `api/hailhq/api/agent_gate.py:22-33`'s existing `RATE_LIMITED_RESPONSES` precedent that this codebase does model response headers in the spec, which is why Task 2 gained its own `GENERAL_RATE_LIMITED_RESPONSES` step). Task 5 (MCP) does not change the OpenAPI surface — the MCP service has no OpenAPI spec at all, it's a separate protocol.

- [ ] **Step 1: Regenerate**

Follow the exact regeneration steps in `docs/public/contributing.md:44-57` (read them first — this plan's summary of "import the app and call app.openapi()" is not necessarily the literal command; use the documented one).

- [ ] **Step 2: Run the CI diff check locally**

Run whatever `.github/workflows/openapi-check.yml:29-42` runs, locally, to confirm the committed file now matches `app.openapi()` with zero diff — read that workflow file for the exact command rather than guessing.

- [ ] **Step 3: Sanity-check the diff**

Run: `git diff openapi/openapi.yaml | head -100` and confirm the diff is plausible. Because Task 1's legacy mount is `include_in_schema=False`, expect every existing path to be _renamed_ to its `/v1/...` form (each old path removed, its `/v1/` counterpart added) rather than doubled — the diff will look large (53 paths renamed) but each operationId should still appear exactly once. Also confirm descriptions are filled in, and that there's no accidental unrelated reformatting of the whole file from a tool-version mismatch. If the diff shows an operationId appearing twice, or touches far more than Tasks 1/3/4's actual changes (e.g. every single line re-indented), stop and report — that's a sign of a generator-version mismatch or a Task 1 regression, not something to paper over by committing anyway.

- [ ] **Step 4: Commit**

```bash
git add openapi/openapi.yaml
git commit -m "chore(openapi): regenerate spec for /v1 versioning + new descriptions"
```

- [ ] **Step 5: Note the CLI follow-up**

`CLAUDE.md` states the CLI codegens its client from this file. Regenerating and releasing the Go CLI (`cli/`) is a separate, manual release step per the repo's own docs (`cli/` ships via GitHub Releases, not this PR's CI) — do not attempt to regenerate or release the CLI as part of this plan. Note in your final report that the CLI's generated client will still target the old unprefixed paths until someone runs its own codegen+release cycle, and that this is expected, not a bug in this task.

---

## Final verification pass (after all tasks)

- [ ] **Step 1: Full test suite across all three packages**

Run: `cd api && uv run pytest -q && cd ../core && uv run pytest -q && cd ../mcp && uv run pytest -q`
Expected: all pass.

- [ ] **Step 2: Lint + format across all three**

Run: `cd api && uv run ruff check . && cd ../core && uv run ruff check . && cd ../mcp && uv run ruff check .` (should be clean already from each task's own Step; this is the final gate) plus `uvx black --check .` in each.

- [ ] **Step 3: mypy**

Run: `cd api && uv run mypy .` (and `core`, `mcp` if they're separately type-checked in CI — check `.github/workflows/ci.yml` for the exact invocation per package).

- [ ] **Step 4: Manual end-to-end smoke test against a locally running API**

Run: `cd api && uv run uvicorn hailhq.api.main:app --reload --port 8080` (needs local Postgres — see `CLAUDE.md`'s dev-commands section), then:

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/v1/whoami -H "Authorization: Bearer $HAIL_API_KEY"
curl -s -D - http://localhost:8080/whoami -H "Authorization: Bearer $HAIL_API_KEY" -o /dev/null | grep -i "deprecation\|link"
curl -s -D - http://localhost:8080/v1/whoami -H "Authorization: Bearer $HAIL_API_KEY" -o /dev/null | grep -i "ratelimit"
```

- [ ] **Step 5: Note in the final summary** that everything here is verified locally; a production re-check (does the deployed api.hail.so and mcp.hail.so actually reflect this) only makes sense after these PRs merge and `deploy.yml` rolls the `api` and `mcp` service images — same caveat as the hail-website branch's own final verification.
