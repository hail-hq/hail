# MCP Resource Server — Phase 1c Design

**Status:** Approved 2026-06-02. Companion to `2026-05-28-mcp-multi-tenant-auth-design.md` (the cross-cutting Phase 1 spec) and `2026-05-28-better-auth-oauth-provider.md` (1b plan, shipped).

## Problem

The Hail MCP service today is a single-tenant pass-through: it boots with `HAIL_API_KEY`, builds one process-singleton `HailClient` pinned to that key, and forwards every tool call against the API with that same shared bearer. Every MCP user resolves to the same API principal — the operator's. Cloud MCP needs per-user, per-org identity.

Phase 1a put JWT verification on `hail/api`. Phase 1b made `hail-website` an OAuth 2.1 Authorization Server. Phase 1c makes MCP a Resource Server in front of the API: it accepts user-bound JWTs at the inbound boundary and forwards them to the API on each tool call. The API stays the single source of JWT validation truth — MCP's job is to surface 401s with the right `WWW-Authenticate` discovery hint and to thread the token onto the outbound call.

## Architecture

Two operator postures, mutually exclusive at boot.

**oauth-rs mode** (cloud). `HAIL_AUTH_URL` is set. FastMCP mounts `AuthSettings` wired to a pass-through `TokenVerifier`. Unauthenticated requests get `401 + WWW-Authenticate: Bearer resource_metadata=${MCP_RESOURCE_URL}/.well-known/oauth-protected-resource`. Tools build a fresh `HailClient(api_key=<bearer>)` per invocation and forward the JWT verbatim to the API; the API verifies signature, issuer, audience, and resolves the org via the `members` join. `HAIL_API_KEY` is empty in this mode.

**static-key mode** (self-host). `HAIL_API_KEY` is set. No FastMCP auth, no protected-resource route. Tools use a module-level `HailClient(api_key=settings.hail_api_key)` — unchanged from today. `HAIL_AUTH_URL` is empty.

Mode is decided at startup from the env. Both set → fail with `ambiguous MCP auth config — set HAIL_AUTH_URL XOR HAIL_API_KEY`. Neither set → fail with `MCP auth not configured`. Env changes require a container restart; this trade is worth the deterministic test surface and the absence of branching in the hot path.

Streamable HTTP is the only transport. The legacy SSE app and `/messages/` endpoint are removed; no deprecation stub, no 410 — MCP clients in production already speak Streamable HTTP after the Phase 0a migration.

## Token flow

Cloud connector (Claude.ai, ChatGPT, IDE):

1. Connector POSTs to `https://mcp.hail.so/` with no `Authorization`.
2. MCP returns `401` + `WWW-Authenticate: Bearer resource_metadata=https://mcp.hail.so/.well-known/oauth-protected-resource`.
3. Connector fetches that metadata. Body: `{"resource": "https://mcp.hail.so", "authorization_servers": ["https://hail.so/api/auth"]}`.
4. Connector fetches `https://hail.so/.well-known/oauth-authorization-server/api/auth` (path-aware RFC 8414 — the route 1b mounted). Finds `authorization_endpoint`, `token_endpoint`, `registration_endpoint`, `jwks_uri`, `code_challenge_methods_supported: ["S256"]`.
5. Connector DCRs at `/api/auth/oauth2/register`, gets `client_id`.
6. Connector redirects user through `/api/auth/oauth2/authorize` with PKCE. User signs in (or is already signed in) and clicks Allow on `/consent`.
7. Connector exchanges code at `/api/auth/oauth2/token`. JWT comes back: `iss=https://hail.so/api/auth`, `aud=https://mcp.hail.so`, `alg=EdDSA`, `exp=now+30d`.
8. Connector retries MCP with `Authorization: Bearer <jwt>`. MCP's pass-through verifier accepts (does not check signature). Tool runs.
9. Tool's per-call `HailClient` forwards the JWT to `https://api.hail.so`. API verifies signature against `${HAIL_AUTH_URL}/jwks`, checks `iss == HAIL_AUTH_URL`, `aud ∈ HAIL_AUTH_AUDIENCES`, resolves `members.organization_id` from JWT `sub`. Returns the org's data.

The pass-through verifier is deliberate: MCP holds no JWKS cache, no issuer config, no signing-algorithm pin. Adding those would duplicate `hail/api`'s logic and create a key-rotation race where the two services briefly disagree. With a single verifier, the API's 401 on a bad token surfaces through MCP unchanged.

## Components

`mcp/hailhq/mcp/server.py` — env-driven mode dispatch in `_build_app()`. Two FastMCP configs: cloud (with `auth=AuthSettings(...)`, `token_verifier=PassThroughVerifier(...)`, protected-resource routes), self-host (today's shape, minus SSE). Drop the SSE branch; remove `mcp_app.sse_app()` and the splat of its routes into the parent Starlette app. Healthz stays.

`mcp/hailhq/mcp/auth.py` _(new)_ — `PassThroughVerifier(TokenVerifier)`. `verify_token(token: str)` returns `AccessToken(token=token, client_id="<opaque>", scopes=[])`. No signature work; no expiry check. ~15 LOC. The verifier exists to satisfy FastMCP's auth API and to populate request state with the raw bearer so tools can read it.

`mcp/hailhq/mcp/tools.py` — each tool function gains a `ctx: Context` parameter (FastMCP injects). Helper `_client_for(ctx)` is an async context manager. In oauth-rs mode it reads `ctx.request_context.request.headers["authorization"]`, strips the `Bearer ` prefix, builds a fresh `HailClient`, and closes it on exit. In static-key mode it yields the module-level singleton without closing on exit. Both branches surface a missing/malformed `Authorization` header as `_unauthorized()` — propagated to the agent as a structured error. Tools call `async with _client_for(ctx) as client:` uniformly, oblivious to which mode is active.

`mcp/hailhq/mcp/hail_client.py` — unchanged. The `api_key` constructor arg already accepts any opaque bearer; in oauth-rs mode it carries a JWT, in static-key mode it carries `hl_live_*`.

`mcp/.env.example` — drop `HAIL_API_KEY` from the cloud section, add `HAIL_AUTH_URL`, `HAIL_AUTH_AUDIENCES`, `MCP_RESOURCE_URL`. The self-host section keeps `HAIL_API_KEY`.

## Scope enforcement

Not in 1c. FastMCP's `AuthSettings.required_scopes` stays empty. Tokens must be present and JWT-shaped at the outbound API call — that's it. The API's `Principal` model carries `scopes` already, but no route enforces them. Scope-aware enforcement is Phase 2 work and lands in the same change that introduces a Hail scope vocabulary.

## Testing

`mcp/tests/test_server_mode_dispatch.py` _(new)_ — boot with `HAIL_AUTH_URL` only ⇒ oauth-rs config; boot with `HAIL_API_KEY` only ⇒ static-key config; both set ⇒ raises at startup; neither set ⇒ raises at startup.

`mcp/tests/test_oauth_rs_auth.py` _(new)_ — using `httpx.ASGITransport(app=app)`:

- GET `/` with no `Authorization` ⇒ 401 with `WWW-Authenticate` containing `Bearer resource_metadata=${MCP_RESOURCE_URL}/.well-known/oauth-protected-resource`.
- GET `/.well-known/oauth-protected-resource` ⇒ 200 with `{"resource": MCP_RESOURCE_URL, "authorization_servers": [HAIL_AUTH_URL]}`.
- Tool invocation with `Authorization: Bearer <opaque>` ⇒ tool's `HailClient` POSTs to a mocked API with the same `Authorization` header verbatim.

`mcp/tests/test_static_key_unchanged.py` _(new)_ — tools use the singleton client; outbound `Authorization` is `Bearer ${HAIL_API_KEY}`; no protected-resource route is mounted; no 401 on missing inbound bearer.

`mcp/tests/test_tools_existing.py` — existing tool-layer tests keep passing in both modes (run the suite under each env config). The `Context` parameter addition must not break tools' input/output contract.

## Operator surface

Cloud deploy env additions to `mcp/.env.example`:

```
HAIL_AUTH_URL=https://hail.so/api/auth
HAIL_AUTH_AUDIENCES=https://mcp.hail.so
MCP_RESOURCE_URL=https://mcp.hail.so
```

Self-host stays exactly as today (one var, no changes):

```
HAIL_API_KEY=<the operator's shared key>
```

Compose changes: the existing `mcp` service's `environment:` block adopts the three new vars in cloud overlays; self-host overlays drop `HAIL_AUTH_URL` and `HAIL_AUTH_AUDIENCES` so the boot-time mode check picks static-key.

The cloud OAuth flow end-to-end requires three pieces to all be deployed: `hail-website` 1b (the AS, already shipped), `hail/api` 1a with `HAIL_AUTH_URL`/`HAIL_AUTH_AUDIENCES` set (already shipped, post the env rename in this session), and `hail/mcp` 1c (this design). Until 1c ships, the cloud deploy answers OAuth discovery from the AS but the MCP boundary has no `WWW-Authenticate` discovery hint to send the client to that AS — the connector flow is broken.

## Out of scope

Documentation and website-card updates (`hail/docs/setup/mcp.md`, `hail-website/app/mcp/clients.ts`, homepage `CodePanel.tsx`) land in a follow-up plan after 1c so the documented flow actually exists. Per-tool scope enforcement, per-org rate limits, and audit logging of MCP-mediated API calls are Phase 2.

## Cross-cuts

- The MCP server publishes `oauth-protected-resource` metadata itself; `hail-website` (the AS) does not. This is by RFC 9728 — the resource server owns its own discovery document.
- `MCP_RESOURCE_URL` must be the value `hail-website` uses for `validAudiences` (set in `lib/auth.ts`'s `oauthProvider({ validAudiences: [...] })`). Mismatch means tokens have an `aud` the API rejects.
- `HAIL_AUTH_URL` is the same value on `hail/api` and `hail/mcp` cloud deployments. It must equal `${HAIL_BASE_URL}/api/auth` on the website — Better Auth's `withPath()` appends `/api/auth` to its base.
