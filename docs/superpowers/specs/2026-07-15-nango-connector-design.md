# Hail → Nango connector — Design

Add Hail to [Nango](https://nango.dev)'s connector catalogue as two provider
entries: `hail` (the REST API) and `hail-mcp` (the remote MCP server).

## Summary

Nango is an integrations platform whose connector catalogue is a single
committed `providers.yaml`. Contributing means a PR to `NangoHQ/nango` adding
a provider block plus a docs page. Nango has no "type" field — a product with
both an API and an MCP server ships **two entries** (`slack` + `slack-mcp`,
`linear` + `linear-mcp`), so Hail does the same.

Work happens in the existing fork `r13i/nango` (`/Users/r/playground/nango`,
branch `master`). The spec lives here, in the `hail` repo, deliberately: a
`docs/superpowers/specs/` file inside the nango checkout would land in the
upstream PR diff.

Related: `2026-07-06-registry-submissions-design.md`. Nango is **not** in that
spec's target list, so this is a new target rather than a duplicate. Two of its
policies bind here anyway — see Decisions #6 and #7.

## Decisions

1. **Two entries, not one.** `hail` (`auth_mode: API_KEY`) and `hail-mcp`
   (`auth_mode: MCP_OAUTH2`). One entry per auth surface is the catalogue's
   convention.
2. **Categories: `communication`, `dev-tools`, `marketing`** — plus `mcp` on the
   MCP entry only. Evidence: of 872 categorised providers, 583 use exactly one
   category, 232 use two, and only 3 use five; none use six. `popular` is
   curated by Nango and is never self-assigned. `other` is a catch-all that only
   3 of its 45 users pair with a real category, so it is excluded — `communication`
   applies, therefore `other` contradicts it. `marketing` is justified by the
   email surface (`/email-domains`, `/sms/suppressions`, `/unsubscribe`), which
   is why SendGrid carries it.
3. **Cloud-only `base_url`,** hardcoded to `https://api.hail.so`. 72 providers
   template `base_url` from `connection_config` to support self-hosting, but
   that forces every Cloud user to type a hostname they don't care about. Hail
   is self-hostable (AGPLv3), so if demand appears the answer is a separate
   `hail-self-hosted` entry, not friction on the common path.
4. **Verification is a fallback chain,** listing `/calls`, `/sms`, `/emails`.
   `credentialsTest` (`packages/server/lib/hooks/hooks.ts:468`) returns on the
   **first** endpoint that answers 2xx — it is not a coverage list, and extra
   endpoints do not broaden testing. The chain is insurance: no route enforces
   scopes today (there are zero scope checks in `api/hailhq/api/routes/`), but
   `mcp/hailhq/mcp/server.py:54` says scope enforcement is "deferred to Phase 2".
   Once it lands, a key scoped to `sms:write` would fail a `/calls`-only probe
   and be rejected despite being valid. Hail's multi-channel surface is conveyed
   in the docs page, which is where readers actually learn it.
5. **`default_scopes: [offline_access]`,** and `registration_params` pinning
   `grant_types`. Both are load-bearing; see §2.
6. **No PR is opened autonomously.** Push the branch; a human opens the PR.
   This matches the registry-submissions non-goal ("no auto-opening PRs against
   third-party repos") and the repo-wide never-create-PRs rule.
7. **Docs follow the feature-claim policy** from `2026-07-06-registry-submissions-design.md`:
   core capabilities (voice, SMS, email) are written present-tense as shipped;
   **provider breadth stays honest to current state** — name a carrier only if
   it is actually wired up in `core/hailhq/core/providers/<channel>/`.

## 1. The `hail` entry (API)

```yaml
hail:
  display_name: Hail
  categories:
    - communication
    - dev-tools
    - marketing
  auth_mode: API_KEY
  proxy:
    base_url: https://api.hail.so
    headers:
      authorization: Bearer ${apiKey}
    verification:
      method: GET
      endpoints:
        - /calls
        - /sms
        - /emails
  docs: https://nango.dev/docs/api-integrations/hail
  docs_connect: https://nango.dev/docs/api-integrations/hail/connect
  credentials:
    apiKey:
      type: string
      title: API Key
      description: Your Hail API key, created at hail.so/console
      pattern: "^hl_live_[A-Za-z0-9_-]+$"
      example: hl_live_************
```

Auth is `Authorization: Bearer <api-key>` (`api/hailhq/api/deps.py:92`). The
`hl_live_` prefix is set by Better Auth's apiKey plugin in
`hail-website/lib/api-keys.ts:52` — not the `api/tests/conftest.py` fixture,
which merely mirrors it.

Placement in `providers.yaml` is alphabetical (after `gusto`).

## 2. The `hail-mcp` entry (MCP)

```yaml
hail-mcp:
  display_name: Hail (MCP)
  categories:
    - communication
    - dev-tools
    - marketing
    - mcp
  auth_mode: MCP_OAUTH2
  client_registration: dynamic
  authorization_url: https://hail.so/api/auth/oauth2/authorize
  token_url: https://hail.so/api/auth/oauth2/token
  registration_url: https://hail.so/api/auth/oauth2/register
  default_scopes:
    - offline_access
  registration_params:
    grant_types:
      - authorization_code
      - refresh_token
    response_types:
      - code
  token_params:
    grant_type: authorization_code
  refresh_params:
    grant_type: refresh_token
  proxy:
    base_url: https://mcp.hail.so
    headers:
      accept: application/json,text/event-stream
  docs: https://nango.dev/docs/api-integrations/hail-mcp
```

Endpoints come from Hail's live RFC 8414 metadata at
`https://hail.so/.well-known/oauth-authorization-server/api/auth` (mounted
path-aware; see `hail-website/app/.well-known/.../route.ts`).
`client_registration: dynamic` follows from `allowDynamicClientRegistration: true`
in `hail-website/lib/auth.ts:332`.

### Why `registration_params` is mandatory

Nango's `registerClientId` (`packages/shared/lib/clients/mcp.client.ts:33-38`)
builds its DCR body from `redirect_uris`, `token_endpoint_auth_method`,
`client_name`, and **only** the `response_types` / `grant_types` keys it filters
out of `registration_params`. With no `registration_params`, Hail registers the
client with `grant_types: ['authorization_code']` — **no `refresh_token`**
(verified against live DCR). The connection would then work for 30 days and fail
permanently at first refresh, long after the PR merged.

### Why `default_scopes: [offline_access]`

Better Auth issues a refresh token only when `offline_access` is requested —
`isRefreshToken = user && (... scopes.includes("offline_access"))`
(`@better-auth/oauth-provider/dist/index.mjs:558`). `default_scopes` prefills
`config.oauth_scopes` (`packages/shared/lib/services/config.service.ts:131`),
which `mcpOauth2Request` joins with `scope_separator` (space default) into the
authorize URL. Better Auth requires PKCE with `offline_access`; Nango always
sends PKCE unless `disable_pkce` is set.

Nango's DCR omits `scope`, and Better Auth then grants the client its full
default set (`openid profile email offline_access`), which contains
`offline_access` — so registration and authorize agree. This matters: a client's
registered scope constrains what it may request
(`validScopes = new Set(client.scopes ?? opts.scopes)`), so a narrower
registration would cause `invalid_scope` at authorize.

### Fields deliberately omitted

- **`scope:`** is not in the schema (`additionalProperties: false`). The correct
  field is `default_scopes`; 51 providers use it, 0 use `scope`.
- **`authorization_params: response_type: code`** is dead config — `response_type`
  is in `reservedOAuthKeys` (`oauth.controller.ts:944`) and filtered before use.
  `linear-mcp` carries it regardless.

### Audience / trailing slash

Nango sends RFC 8707 `resource` from the protected-resource metadata
(`oauth.controller.ts:1079-1087`), i.e. `https://mcp.hail.so/` **with** a
trailing slash. Hail's `validAudiences` uses `urlVariants(mcpResourceUrl)` and
accepts both forms, so this is already handled — but it is the exact failure
mode recorded in the "URLs are not strings" rule, so any change here must
re-verify rather than assume.

## 3. Docs

Following the `linear-mcp` / `resend` pattern, in the nango checkout:

- `docs/api-integrations/hail.mdx`
- `docs/api-integrations/hail/connect.mdx`
- `docs/api-integrations/hail-mcp.mdx`
- Register all three in `docs/docs.json`, alphabetically, as
  `api-integrations/<slug>` (the `integrations/all/<slug>` form is legacy).

Content is subject to Decision #7. The multi-channel surface (voice, SMS,
email) is described here rather than implied through verification endpoints.

## 4. Testing

- `npm run test:providers` — schema validation
  (`scripts/validation/providers/validate.ts`).
- Local Docker Compose per Nango's contributing guide: create both integrations
  in the UI and establish a real connection each.
  - `hail`: paste a live `hl_live_` key; verification must pass.
  - `hail-mcp`: complete the OAuth flow in a browser and **confirm a
    `refresh_token` is actually returned**. This is the one claim not yet
    verified — probing stops at the login page, which needs a human.
- Nango's guide requires real testing before submission; schema validation alone
  proves neither the proxy nor the OAuth flow.

### Already verified against live Hail

- `GET /calls`, `/sms`, `/emails` with a real key → 200; unauthenticated and
  junk-key → 401 (valid verification endpoints).
- DCR against `https://hail.so/api/auth/oauth2/register` → client issued.
- Authorize accepts `scope=offline_access` → 302 to `/signin`.
- `resource=https://mcp.hail.so/` accepted.

## 5. Repo chores

- Branch in `r13i/nango` off `master`, prefixed per gitflow (e.g.
  `feat/add-hail-provider`).
- No new env vars; nothing in `hail`/`hail-website` changes.

## Out of scope

- `hail-self-hosted` (templated `base_url`) — Decision #3.
- Nango **integration templates** (pre-built syncs/actions); this is catalogue
  registration only.
- Adding Nango to the `2026-07-06-registry-submissions-design.md` target list —
  worth doing, but that spec's Phase 1/Phase 2 workflow is a separate effort.

## Follow-ups (not blocking)

- **Prod `oauth_clients` cleanup.** Live DCR testing created 4 junk client rows,
  all named `...(delete me)`: `QxFRuVYz…`, `qKUWIwUs…`, `rpWTAOgH…`,
  `yoOFiAwN…`. DCR returns no `registration_access_token`, so RFC 7592 deletion
  is unavailable; they need removing directly.
- **`allowUnauthenticatedClientRegistration: true`** (`hail-website/lib/auth.ts:333`)
  lets anyone write unbounded rows to `oauth_clients` with no credentials. This
  is spec-conformant for MCP and Linear does the same, but the abuse surface is
  worth a deliberate decision rather than a default.
