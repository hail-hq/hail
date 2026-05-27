# Hail MCP Server Roadmap — to Managed Cloud

- **Date**: 2026-05-26
- **Status**: Approved design (brainstorming output). Each phase below becomes its own spec → plan → implementation cycle.
- **Scope**: Strategic roadmap across five workstreams. This is _not_ a single implementation spec — it sequences the work and fixes the cross-cutting decisions so individual specs can be written against a shared target.

## North star

Ship `mcp.hail.so` as a **multi-tenant, commercially-operated** MCP server. An agent connects as a _specific Hail org_, and that identity flows through to the Hail API. Self-host (`docker compose up`) stays first-class via a simpler shared-key mode — but cloud drives the sequencing.

## Current state (grounded in code)

| Axis           | Today                                                                                                                                        | File                                                                   |
| -------------- | -------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------- |
| Transport      | SSE only (`mcp_app.sse_app()`), deprecated by the MCP spec in favor of Streamable HTTP                                                       | `mcp/hailhq/mcp/server.py:24`                                          |
| Auth / tenancy | **One shared `HAIL_API_KEY`** injected server-side on every upstream call; the caller's bearer is never read. Single-tenant by construction. | `mcp/hailhq/mcp/hail_client.py:58-64`, `core/hailhq/core/config.py:97` |
| Tool coverage  | 5 tools: `place_call`, `send_email`, `get_call`, `list_calls`, `get_events`                                                                  | `mcp/hailhq/mcp/tools.py`                                              |
| Contract       | `HailClient` **hand-encodes** the wire contract that `openapi/openapi.yaml` already owns → drift risk                                        | `mcp/hailhq/mcp/hail_client.py`                                        |
| Tool quality   | Good docstrings, but untyped `dict[str, Any]` returns and no MCP tool annotations                                                            | `mcp/hailhq/mcp/tools.py`                                              |
| Hardening      | One unit-test file; no auth/transport/integration tests; cloud endpoint not live                                                             | `mcp/tests/test_tools.py`                                              |

The single most architecturally-wrong thing today: the per-client bearer token in our setup docs does nothing for the self-hosted server — the server authenticates upstream with its own env key. That is exactly why `mcp.hail.so` (a multi-tenant product) cannot exist yet.

## Target ("best of worlds")

A multi-tenant, **OAuth-authenticated** (Better Auth), **Streamable-HTTP** server whose tool layer is **generated from `openapi/openapi.yaml`**, exposing Hail's full surface (voice + SMS + email + inbound + number management) as well-annotated **tools, resources, and prompts**, with per-tenant rate limits and tracing, and the cloud endpoint live.

## Decisions locked

1. **Auth = Better Auth.** `hail-website` (which already runs Better Auth — `@better-auth/api-key`, the `[...all]` auth route, CLI `issue-key`) becomes the **OAuth 2.1 Authorization Server** via the `oauth-provider` + `mcp` plugins. The Python MCP server is a **Resource Server**.
2. **Transport = Streamable HTTP**, with `/sse` kept alive through a transition window.
3. **Pluggable auth**: `static-key` mode (self-host default) vs `oauth-rs` mode (cloud). Self-host never requires running Better Auth + a login UI.
4. **Contract from `openapi/openapi.yaml`** — generate the tool/client layer rather than hand-encoding it.
5. **Sequencing = quick-wins-first**: land Phase 0 (dependency-free) while the auth cross-service work is scoped and built.

## Workstreams

### Phase 0 — Foundation (parallel, low-risk, no external deps)

**0a. Transport: SSE → Streamable HTTP**

- _Goal_: modern client compatibility; stop riding a deprecated transport.
- _Scope_: swap `sse_app()` → `streamable_http_app()` in `server.py`; keep the `/sse` route mounted during a transition window; update `docs/setup/mcp.md` and the `hail-website` client snippets once the new URL is live.
- _Dependencies_: none.
- _Done_: server serves Streamable HTTP; existing clients still work via `/sse`; healthcheck unchanged.

**0b. Contract-driven tool layer**

- _Goal_: eliminate hand-encoded drift before tool coverage grows.
- _Scope_: generate `HailClient` request/response models and tool schemas from `openapi/openapi.yaml`; keep the agent-facing docstrings/error mapping that make the tools good for agents.
- _Dependencies_: `openapi.yaml` stays the source of truth (already an invariant).
- _Done_: adding an API endpoint surfaces in the MCP tool layer without hand-writing wire contracts; no behavioral change to the existing 5 tools.

### Phase 1 — Multi-tenant auth (the cloud unlock; critical path)

**1a. Hail API accepts per-org identity** ⚠️ _gating dependency, largest chunk_

- _Goal_: the API can act on a request authenticated as a specific org, not just the shared key.
- _Scope_: decide and build one of — (i) the API validates the Better Auth JWT directly, or (ii) the MCP server exchanges the JWT for an org-scoped internal credential the API already understands. Define the org/scope model.
- _Dependencies_: none external, but it is the prerequisite for 1b/1c to deliver value.
- _Done_: a request carrying org identity X performs actions as org X, isolated from org Y.

**1b. hail-website = Authorization Server**

- _Goal_: give MCP clients the native OAuth flow they expect.
- _Scope_: enable Better Auth `oauth-provider` + `mcp` plugins — Dynamic Client Registration (incl. unauthenticated public-client registration for agents), discovery metadata (`.well-known/oauth-authorization-server`, `.well-known/oauth-protected-resource`), JWT issuance verifiable at `/jwks`, consent UI, and **audience binding (RFC 8707)** so a token is bound to the MCP server as its resource.
- _Dependencies_: none (separate codebase); coordinates with 1c on issuer/audience/claims.
- _Done_: Claude/ChatGPT cloud connectors complete DCR → consent → token against `hail-website`.

**1c. Python MCP = Resource Server**

- _Goal_: authenticate per connection and forward that identity upstream.
- _Scope_: pluggable auth (`static-key` | `oauth-rs`); in `oauth-rs` mode validate the JWT locally via JWKS (issuer + audience + scopes), extract org/scopes, and build a **per-request** `HailClient` with that identity (replacing the module-level singleton in `server.py:33`). **Accept both** OAuth JWT and a pasted Better Auth API key.
- _Dependencies_: 1a (per-org identity upstream), 1b (issuer/JWKS/audience).
- _Done_: two different orgs' tokens hit the same server and act as their own org; self-host still works with `static-key`.

### Phase 2 — Product surface

- _Expand tools_: SMS, inbound (`recv`/`thread`/`inbox`), number management — **each gated on its API endpoint existing**.
- _Add MCP resources_ (calls/events/transcripts as readable data) and _prompts_ for common workflows.
- _Tool quality_: `readOnlyHint`/`destructiveHint` annotations; **structured output schemas** replacing `dict[str, Any]`.
- _Done_: full Hail surface is reachable from an agent with correct read/write hints and typed outputs.

### Phase 3 — Hardening + GA

- Per-tenant rate limits/quotas; structured logging + tracing; an explicit error taxonomy.
- Integration tests: auth, transport, **tenant isolation**.
- Go live: `mcp.hail.so` deploy (the `mcp.${HAIL_DOMAIN}` vhost already exists in `Caddyfile`), scaling, monitoring.
- _Done_: `mcp.hail.so` is a supported, monitored, multi-tenant endpoint.

## Sequencing & critical path

```
Phase 0  ──────────────► (independent quick wins, land first)
  0a Transport
  0b Contract-gen

Phase 1  1a API per-org identity ──► 1b Better Auth AS ──► 1c Python RS   ◄── CRITICAL PATH TO CLOUD
                                  └─► (1b can build in parallel; value gated on 1a)

Phase 2  (gated on API endpoints + Phase 1 for tenant-scoped data)
Phase 3  (gated on Phases 1–2)
```

**Critical path to cloud is 1a → 1c.** Phase 0 is pure upside that lands independently. Phase 2 is gated on the API shipping the underlying endpoints.

## Cross-cutting constraints

- **AGPLv3**; any new dependency must be license-compatible.
- **Self-host must keep working** without Better Auth — `static-key` mode is the default there.
- Repo tenets: simple code (no abstraction without two concrete uses), agent-first docs, **`openapi/openapi.yaml` is the source of truth** for the API contract.

## Risks & open questions (resolve in per-phase specs)

- **1a is the real cost.** Better Auth makes the client-facing OAuth trivial; the value only lands when the Hail API can act on a per-org token. Pick (i) API-validates-JWT vs (ii) token-exchange early — it shapes 1c.
- **Org/scope model**: what scopes exist (e.g. `calls:write`, `events:read`), and how they map to tool access.
- **Transition windows**: how long `/sse` stays mounted (0a); how long `static-key` remains accepted on cloud during rollout (1c).
- **API-key vs OAuth UX**: confirm terminal clients keep the pasted-key path while connectors use OAuth.

## Decisions

- **MCP stays a separate service from `api/`** (not merged or mounted in-process). MCP holds long-lived streaming connections with a different scaling/failure profile than the transactional REST API; transport churn (e.g. the 0a SSE→Streamable HTTP migration) should not redeploy the core API; and MCP is an adapter over the _public_ API surface, which preserves "OpenAPI is the source of truth" and lets MCP own protocol concerns (transport, the 1c OAuth resource-server, tool annotations). The drift and shared-key irritants that motivate merging are addressed by Phase 0b (shared contract) and Phase 1 (auth) instead. Revisit only if independent scaling proves unnecessary and one-service ops simplicity dominates.

## Next steps

Write per-phase specs in priority order, each through the normal brainstorming → writing-plans cycle:

1. Phase 0a (transport) — smallest, fastest feedback.
2. Phase 0b (contract-gen).
3. Phase 1a (API per-org identity) — start scoping in parallel; it is the long pole.
