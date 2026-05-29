# MCP Multi-Tenant Auth (Phase 1) — Design

- **Date**: 2026-05-28
- **Status**: Approved design (brainstorming output).
- **Roadmap**: Phase 1 of [2026-05-26-mcp-server-roadmap-design.md](2026-05-26-mcp-server-roadmap-design.md). Covers 1a (API per-org identity — small extension), 1b (Better Auth as OAuth AS), 1c (MCP as pure forwarder) end-to-end.
- **Scope**: `hail-website` + `api/` + `mcp/`. No transport change (uses the 0a Streamable HTTP at root). Implementation will split into **three per-service plans** (one per codebase); this spec is the cross-cutting design they share.

## Goal

A user connects a client (Claude.ai web Connectors, ChatGPT, Claude Code, the `hail` CLI, …) to Hail and acts **as their own org** — not as the deployment's shared identity. Self-host (`docker compose up`) keeps the simple shared-key story.

## Current state (the surprise)

The roadmap framed 1a — the API accepting per-org identity — as the "long pole, gating dependency." It isn't: **the API already does per-org auth.** `api/hailhq/api/deps.py:195–211` (`get_current_principal`) has two paths today:

- **Shared-key path** (self-host): bearer compared HMAC-style to `settings.hail_api_key`; returns `Principal(api_key_id=None, organization_id=SELF_HOSTED_ORG_ID, scopes=["*"])`.
- **Per-user API-key path**: SHA-256 hashes the bearer; DB-looks up Better Auth's `api_keys` table; outer-joins `members` to resolve `organization_id`; parses `permissions` JSON into scopes; returns `Principal(api_key_id=…, organization_id=…, scopes=…)`.

Routes already scope rows by the principal — `calls.py:212` sets `Call.organization_id = principal.organization_id`, list queries filter on it, audit logs carry it. `hail-website` already issues Better Auth API keys via `app/api/cli/issue-key/route.ts` (CLI device flow). The Better Auth schema already has `users`, `sessions`, `accounts`, `api_keys`, `organizations`, `members`, `invitations`, `device_codes` — see `hail-website/lib/auth.ts` and `better-auth_migrations/*.sql`.

What's actually missing for Phase 1:

| Piece                                                                          | Status                                       |
| ------------------------------------------------------------------------------ | -------------------------------------------- |
| API resolves per-org identity from a bearer                                    | done (API-key DB lookup)                     |
| Routes scope rows by `principal.organization_id`                               | done                                         |
| `hail-website` issues per-user credentials                                     | done (Better Auth API keys, CLI device flow) |
| MCP forwards the caller's bearer (drops `HAIL_API_KEY` server-side)            | **not done — 1c (the real long pole)**       |
| Better Auth issues OAuth JWTs (for web Connectors that do DCR, not paste keys) | **not done — 1b**                            |
| API validates JWTs (alongside API keys)                                        | **not done — small 1a extension**            |
| Scope enforcement at routes (scopes parsed today but never checked)            | not done — deferred to Phase 2               |

## Decisions locked

1. **API validates JWTs _in addition to_ API keys.** MCP forwards the caller's bearer; the API validates whichever it is. No token-exchange flow in MCP. (Alternatives considered: MCP exchanges JWT → API key; or skip JWTs entirely. Rejected: extra state for no real gain; loses native web-Connector OAuth UX.)
2. **MCP is a pure forwarder.** No `HAIL_API_KEY` env var on the MCP service; no mode switch; the module-level `HailClient` singleton goes away. Per-request `HailClient` built from the incoming `Authorization`. Self-host operators paste `HAIL_API_KEY` as their _client_ bearer; cloud users paste their API key or use OAuth — the API's existing dual-path handles both.
3. **OAuth via Better Auth.** `oauth-provider` + `mcp` + `jwt` plugins on `hail-website`. JWT issuance verifiable via `/jwks`. DCR with unauthenticated public-client registration enabled (for MCP agents). PKCE mandatory. Audience-bound (RFC 8707).
4. **Scope enforcement deferred to Phase 2.** Cloud ships with the existing "any valid key → full org access" until then; the infrastructure to add per-scope route checks already exists (`Principal.scopes`).
5. **Single cross-cutting spec, three per-service plans.** Implementation work splits by codebase; the plans share this spec as their shared target.
6. **Token lifetimes — as long as the spec and clients tolerate.** Better Auth's `oauth-provider` plugin is configured with the longest TTLs OAuth 2.1 allows, so users re-consent infrequently. Targets: **access token ≥ 30 days, refresh token ≥ 180 days, sliding renewal, no idle timeout**. JWTs always carry an `exp` (PyJWT requires it; the OAuth 2.1 spec requires it for access tokens); the equivalent of "infinite" is "very long token + refresh that the client renews silently." Revocation is via the console "Authorized apps" panel (Better Auth's `oauth-provider` exposes the deny-list). Concrete plugin options (`accessTokenExpiresIn` / `refreshTokenExpiresIn` or equivalent) are verified and set in the 1b plan.

## Topology

```
Client ── Bearer <key|JWT> ──► MCP ── forwards bearer ──► API
                                │
                                └─ OAuth discovery points clients at ──► hail-website (Better Auth AS)
                                                                          │  (DCR + PKCE + JWT, audience-bound)
                                                                          └─ /jwks  ─── verified by ──► API
```

Boundaries:

- **`hail-website`**: identity + token issuance. Owns users, orgs/members, API keys (today), and OAuth (new). Sole holder of signing keys.
- **`api/`**: authorization decisions. Sole place that turns a bearer into a `Principal` and scopes data access by `organization_id`. Validates both API keys (DB lookup) and JWTs (signature + claims).
- **`mcp/`**: MCP protocol wire (transport, tool dispatch) + OAuth _discovery_. **Not** an authorization decision point: it does not validate the bearer itself, only forwards it and translates the API's 401 into the MCP-spec `WWW-Authenticate` challenge.

## Per-service design

### 1b — `hail-website` becomes the Authorization Server

- **Plugins to add**: Better Auth `oauth-provider`, `mcp`, and `jwt` (the last so tokens are JWTs verifiable via JWKS rather than opaque-with-introspection).
- **Discovery routes** (added by the `mcp` plugin or wired manually): `/.well-known/oauth-authorization-server` (RFC 8414) and `/.well-known/oauth-protected-resource` (the latter as a _proxy_ MCP clients can hit when they misparse the resource hint). The Better Auth `mcp` plugin documents this proxying explicitly.
- **JWT shape**: standard claims plus `sub` = user_id (UUID, same column the API's `members` join uses against `api_keys.reference_id`). `aud` = the resource the token was minted for; default `https://mcp.hail.so` when issued for the MCP server. `iss` = the Better Auth base URL. JWKS exposed at `/jwks`.
- **DCR**: `allowDynamicClientRegistration: true` _and_ `allowUnauthenticatedClientRegistration: true` — the latter is what Better Auth's own docs flag as "useful for MCP agents."
- **Consent**: standard Better Auth consent UI (off-the-shelf); no custom screen for Phase 1.
- **What does NOT change**: the `apiKey` plugin stays (CLI/terminal credential remains a Better Auth API key with the existing `hl_live_` prefix); the org / members / sessions / device-codes schemas stay; the `issue-key` route stays. Existing CLI flow is unchanged.

### 1a — API gains a JWT path (`api/hailhq/api/deps.py`)

- New module-level helper: `_principal_from_jwt(token: str, db: AsyncSession) -> Principal`.
- `get_current_principal` becomes: try `_check_shared_key` → if no, try `_principal_from_apikey_table` (table-existence check unchanged) → if neither matched, try `_principal_from_jwt`. Tokens that match neither shape produce the existing 401 `"invalid API key"` (or a generalized `"invalid bearer"`).
- **JWT verification**:
  - Decode the bearer; if it doesn't look like a JWT (3 dot-separated segments), skip the JWT path.
  - Fetch JWKS from `BETTER_AUTH_JWKS_URL` (e.g. `https://hail.so/api/auth/jwks`) on startup; cache parsed keys; refresh every N minutes (default 15) or on signature failure (one retry on `kid` miss).
  - Verify signature against the matching `kid` from JWKS.
  - Verify `iss` equals `BETTER_AUTH_ISSUER` env value.
  - Verify `aud` is in `ALLOWED_AUDIENCES` (env, comma-separated; default `["https://api.hail.so", "https://mcp.hail.so"]`).
  - Verify `exp` not in the past (with a small leeway).
  - Resolve `organization_id` via the same `members` join used by the API-key path (`members.user_id = <jwt.sub>`). Same 403 "user not provisioned" if missing.
  - Parse scopes from a `scopes` (or `scope`) JWT claim if present; default `["*"]` if absent.
- **Caches**: process-local JWKS cache (no Redis). Failure-mode: if JWKS fetch fails on startup, the JWT path is disabled until the next refresh succeeds; API-key path still works.
- **New library deps**: a JWT lib (PyJWT is the standard choice for FastAPI; already license-compatible with AGPLv3). One new dep, well-vetted.
- **New env vars** (added to `.env.example` per repo invariant): `BETTER_AUTH_ISSUER`, `BETTER_AUTH_JWKS_URL`, `ALLOWED_AUDIENCES` (CSV). Empty / unset → JWT path disabled (self-host stays simple).

### 1c — MCP becomes a pure forwarder (`mcp/hailhq/mcp/server.py` + `hail_client.py`)

- **Drop the module-level singleton**. Today `server.py:66` creates `hail_client = HailClient()` at import time using the MCP service's own `settings.hail_api_key`. Remove this; build a `HailClient` **per request** using the inbound `Authorization` header.
- **Per-request `HailClient`**: each tool invocation needs the request's bearer. FastMCP exposes the incoming request via context. The simplest pattern: in `register_tools`, the closures pull the current request's `Authorization` header from the FastMCP context (or via Starlette's contextvars set by `RequireAuthMiddleware`), construct a `HailClient(api_key=<token>)`, run the tool, close the client. The per-request client's `Authorization` is whatever the client sent — API key or JWT — forwarded verbatim.
- **OAuth discovery on the MCP server**:
  - Configure FastMCP with `auth = AuthSettings(resource_server_url="https://mcp.hail.so", issuer_url=<BETTER_AUTH_ISSUER>, ...)` and a `token_verifier`.
  - The verifier is a **trivial pass-through**: it does _not_ validate the JWT signature (the API does). Its only job is to make FastMCP's middleware return 401 with `WWW-Authenticate: Bearer resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"` on missing-or-empty auth, and to extract the token (for logging/forwarding) on present auth. FastMCP's `create_protected_resource_routes` (which we already see in the SSE/streamable_http app builders) publishes the metadata.
- **API-401 wrapping**: if a forwarded request to the API returns 401, MCP returns 401 + the same `WWW-Authenticate` header to the client. (Mid-session re-auth: the client knows where to re-discover.)
- **Environment**: the MCP service no longer reads `HAIL_API_KEY`. It does need `HAIL_API_URL`, `BETTER_AUTH_ISSUER` (for the AS pointer in discovery metadata), and the public `MCP_RESOURCE_URL` (e.g. `https://mcp.hail.so`).
- **Static-key still works for self-host without ceremony**: an operator pastes `HAIL_API_KEY` into their _client_ and connects to MCP; MCP forwards; the API matches the shared-key path. No mode flag on MCP. (Self-host without MCP — direct CLI to API — is unchanged.)

## Token flows

**Self-host CLI or agent** → bearer = `HAIL_API_KEY`. MCP forwards → API shared-key path matches → `Principal(SELF_HOSTED_ORG_ID, scopes=["*"])`. No behavior change for self-host.

**Cloud CLI** → `hail login` (Better Auth device flow, today) issues a per-user Better Auth API key → CLI saves to `~/.hail/credentials.json` → CLI sends `Bearer <key>` → MCP forwards → API per-user path → real-org `Principal`. CLI flow is unchanged; this _already works for the CLI today calling the API directly_; Phase 1 makes it work through MCP too.

**Cloud web Connector (Claude.ai / ChatGPT)** → first request to MCP without auth → MCP 401 + `WWW-Authenticate: Bearer resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"` → client fetches that → it points at `hail-website` AS → client does DCR (registers as a public, unauthenticated client) → user OAuth consent (Better Auth UI) → AS issues JWT (`aud=https://mcp.hail.so`) → client retries MCP with `Bearer <JWT>` → MCP forwards → API JWT path verifies signature/iss/aud/exp, resolves org via `sub` → real-org `Principal`. New flow.

## User setup UX (what ships in front of the user)

### Cloud users (`mcp.hail.so`)

- **Web Connectors (Claude.ai web, ChatGPT)** — Settings → Connectors → Add custom connector → paste `https://mcp.hail.so` → client does DCR against Better Auth → browser opens for consent → connected. **No pasted keys.** This is the headline UX win.
- **Claude Desktop** — same Connectors UI path as Web (paste URL, browser OAuth). The previous `mcp-remote` empty-`Bearer ` snag goes away because nothing substitutes anything. `mcp-remote` config-file form still works (it handles OAuth itself or accepts a pasted key) but is no longer the recommended path.
- **Claude Code** — two equally clean options:
  - OAuth: `claude mcp add --transport http hail https://mcp.hail.so` (no `--header`; first call triggers a browser consent tab).
  - Pasted key: visit `https://hail.so/console/keys` → "Issue new key" → copy the `hl_live_…` value → `claude mcp add --transport http hail https://mcp.hail.so --header "Authorization: Bearer hl_live_..."`.
- **Cursor / Windsurf / Zed / Copilot (VS Code) / Gemini CLI / Raycast** — most current versions support OAuth MCP (DCR + browser consent). Paste the URL in the client's MCP-server UI; the client does DCR + consent. Pasted-key form remains available everywhere as the universal fallback: visit the console, copy a key, paste it into the client's config alongside the URL (the shape we ship today on `hail.so/mcp`). The per-card plan verifies OAuth support per client at write time.

### Self-host operators

Unchanged from today. `HAIL_API_KEY` in `.env` _is_ the client bearer — paste it into whatever client config you use. No OAuth setup, no `hail-website` running, one env var.

### The console (`hail-website`)

The existing console (`app/console/keys/`) already supports issuing API keys with scopes. Phase 1 surfaces a **new "Authorized apps"** panel listing OAuth client registrations and active tokens (the Better Auth `oauth-provider` plugin gives us the data; the panel is small). Users can revoke an authorized app the same way they revoke a key.

## Documentation deliverables

User-facing copy that has to change when Phase 1 ships. This is a single plan executed _after_ the three service plans land (so the OAuth flow it documents actually exists):

- **`hail/docs/setup/mcp.md`** — promote the **Connectors UI / OAuth** flow as primary for each web/desktop client; demote pasted-bearer to "alternative." Drop the Windows-mangling caveat once OAuth via Connectors UI is the documented path. Update the self-host section to explicitly call out "`HAIL_API_KEY` is your client bearer" (avoids the original confusion about whether the server validates it).
- **`hail-website/app/mcp/clients.ts`** — for each of the 8 cards: keep the URL `https://mcp.hail.so`, switch the method label to the Connectors / OAuth-Add path, update the snippet to either be URL-only (for clients where OAuth is the UI flow) or `type: http` + URL with OAuth-discovery implied (for clients where the JSON config is still primary, e.g. Cursor); keep a short "Alternative: paste a key from the console" note per card.
- **`hail-website/app/mcp/page.tsx`** — update the "Or — point any MCP client at the endpoint" proto-table to mention OAuth as the auth method (in addition to bearer key), and add a Tools/today line for whatever 1c lands with (still `place_call`, `send_email`, `get_call`, `list_calls`, `get_events` unless tool coverage expands).
- **`hail-website/app/components/CodePanel.tsx`** — homepage `mcp.json` panel: the explicit `headers: { Authorization: ... }` shape stays valid (for the pasted-key alternative); the comment block can note OAuth is also supported.
- **`hail-website/app/welcome/WelcomeMain.tsx`** — onboarding: keep `MCP_URL`, update the "8 clients · Streamable HTTP" sub-line if more accurate copy fits ("8 clients · OAuth or bearer").
- **`hail/CLAUDE.md`** and **`hail/docs/architecture.md`** — update the "single-tenant / shared key only" framing to reflect the new dual-token reality. Recent edits already removed most of the stale claims; one paragraph in each will need to acknowledge the OAuth path.

The same plan also updates **`hail-website/AGENTS.md`** if any Next.js conventions changed for the new routes.

## Coordination across the three services

A small set of values must agree across the three codebases. Phase 1 ships them as environment variables; documenting them in `.env.example`:

| Variable               | Read by  | Meaning                                                                                           |
| ---------------------- | -------- | ------------------------------------------------------------------------------------------------- |
| `BETTER_AUTH_ISSUER`   | api, mcp | The Better Auth base URL (string compared against `iss` and embedded in discovery metadata).      |
| `BETTER_AUTH_JWKS_URL` | api      | Where to fetch public keys for JWT verification.                                                  |
| `MCP_RESOURCE_URL`     | mcp      | The public MCP URL used in `WWW-Authenticate` `resource_metadata=…`.                              |
| `ALLOWED_AUDIENCES`    | api      | Comma-separated list of accepted `aud` claims. Default `https://api.hail.so,https://mcp.hail.so`. |

## Tests

- **API**: unit tests for `_principal_from_jwt` covering bad signature, wrong issuer, wrong audience, expired, missing/unprovisioned `sub`, scopes-claim parsing; cached-JWKS refresh on `kid` miss. Integration: a JWT signed by a test keypair flows through `get_current_principal` to the right org. `_principal_from_apikey_table` + shared-key tests unchanged.
- **MCP**: per-request `HailClient` test asserts the _outbound_ `Authorization` matches the inbound (mock httpx). 401 without auth → response is 401 + `WWW-Authenticate` containing `resource_metadata=…`. A forwarded API 401 → same wrapper. The existing 0a route-wiring tests (`/`, `/sse`, `/messages`, `/healthz`) still pass.
- **`hail-website`**: discovery metadata routes return the expected JSON; DCR creates a `client`; a token issued for the MCP audience has `aud=https://mcp.hail.so` and verifies against `/jwks`.

## Out of scope (deferred)

- Route-level scope enforcement (deferred to Phase 2 per the brainstorming decision).
- Per-tenant rate limits / quotas, structured logging + tracing, integration tests across services (Phase 3).
- The cloud GA deploy of `mcp.hail.so` as a supported product (Phase 3).
- Token revocation UI beyond what Better Auth provides out of the box.
- Refresh-token UX in the CLI (CLI keeps its long-lived Better Auth API key for now).

## Done criteria

- `hail-website` issues OAuth 2.1 JWTs with PKCE + DCR; `/.well-known/oauth-authorization-server` and `/.well-known/oauth-protected-resource` resolve; `/jwks` returns the signing keys.
- The API's `get_current_principal` resolves three bearer types — shared key, Better Auth API key, Better Auth JWT — to the same `Principal` shape; the API-key and shared-key paths are byte-for-byte unchanged.
- The MCP server forwards the caller's bearer to the API on every tool call (per-request `HailClient`); no `HAIL_API_KEY` is consulted by MCP; 401 from the API or missing auth on a tool call produces an MCP-spec `WWW-Authenticate` response.
- Two real orgs' tokens hit `mcp.hail.so` and act as their own orgs, isolated from each other. Self-host (`docker compose up` with `HAIL_API_KEY` set) continues to work unchanged for the operator.

## Implementation plan structure

Phase 1 implements as **four plans** — three per service, plus one for the user-facing copy that depends on all three having landed:

1. **API plan** (`docs/superpowers/plans/...-api-jwt-validation.md`) — adds `_principal_from_jwt`, JWKS cache, env vars, tests.
2. **`hail-website` plan** (`...-better-auth-oauth-provider.md`) — enables `oauth-provider` + `mcp` + `jwt` plugins, configures issuer/audience/PKCE/DCR, verifies discovery, adds the console "Authorized apps" panel.
3. **MCP plan** (`...-mcp-bearer-forwarder.md`) — per-request `HailClient`, drop `HAIL_API_KEY` from MCP env, wire pass-through `token_verifier` + protected-resource metadata, 401 wrapping.
4. **Docs + website cards plan** (`...-phase1-docs-and-website-cards.md`) — apply the changes listed in _Documentation deliverables_ above. Runs after 1–3 so it documents reality, not aspiration.

The API plan can land first (no dependencies). The `hail-website` AS plan can build in parallel. The MCP plan needs the AS up to test the consent flow end-to-end but can be written and unit-tested before. The docs plan lands last.
