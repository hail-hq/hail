# Hail → Nango Connector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Hail to Nango's connector catalogue as two provider entries — `hail` (REST API) and `hail-mcp` (remote MCP server) — plus three docs pages, then hand a pushed branch to a human to open the PR.

**Architecture:** Nango's catalogue is a single committed `packages/providers/providers.yaml`; each provider is a YAML block validated by a schema script. A connector also needs `.mdx` docs pages registered in `docs/docs.json`. No application code changes — this is configuration plus docs. Correctness is proven by the schema validator, by live behavioural checks against Hail's production endpoints (the same requests Nango's `credentialsTest` and MCP OAuth paths make), and finally by a real connection through Nango's local Docker Compose UI.

**Tech Stack:** YAML, Mintlify MDX, `docs.json`; Node/`tsx` validator (`scripts/validation/providers/validate.ts`); `curl` for behavioural checks; Docker Compose for the local end-to-end test.

**Working repo:** `r13i/nango` fork at `/Users/r/playground/nango` (a fork of `NangoHQ/nango`). All tasks run there. The design spec and submission tracker live in the separate `hail` repo and are **not** touched by this plan.

**Design source:** `hail` repo → `docs/superpowers/specs/2026-07-15-nango-connector-design.md`. Copy source: `hail` repo → `docs/submissions/nango.md`.

## Global Constraints

- **Alphabetical placement, verified anchors (against `upstream/master`, 2026-07-16).** `hail` and `hail-mcp` both sort **before `haileyhr`** (`hail` is a prefix of `haileyhr`; `hail-mcp`'s `-` precedes `haileyhr`'s `e`), so both insertions land there — NOT before `harvest`. `providers.yaml`: insert the `hail` block then `hail-mcp` immediately before the `haileyhr:` entry (currently line 9991), which follows the `hackerrank-work:` block (line 9963). `docs.json`: insert `"api-integrations/hail"` then `"api-integrations/hail-mcp"` between `"integrations/all/hackerrank-work"` (line 1285) and `"api-integrations/haileyhr"` (line 1286), inside the `"800+ APIs & Integrations"` group (label at line 918). Re-grep every anchor before editing — line numbers drift.
- **Two entries, one per auth surface.** `hail` = `auth_mode: API_KEY`; `hail-mcp` = `auth_mode: MCP_OAUTH2`. Do not merge.
- **Categories are exact.** `hail`: `communication`, `dev-tools`, `marketing`. `hail-mcp`: those three plus `mcp`. No others — no `popular`, no `other`.
- **`hail-mcp` MUST carry `registration_params` with `grant_types: [authorization_code, refresh_token]`** and `default_scopes: [offline_access]`. Omitting either silently breaks token refresh after 30 days. See Task 2 rationale.
- **`scope:` is not a valid field** (`additionalProperties: false`); the correct field is `default_scopes`.
- **Docs path is `api-integrations/<slug>`**, matching `resend` and `linear-mcp`. The CONTRIBUTING doc says `integrations/all/` and group-order "alphabetical" — that is the legacy path; follow the modern `api-integrations/` convention actually used by current comms/MCP peers.
- **Description copy is fixed** (verified against 364 existing pages — no `&`, no em-dash, no trailing period, under 141 chars):
  - `hail.mdx`: `Integrate your application with the Hail API for phone, SMS, and email for AI agents and humans`
  - `hail-mcp.mdx`: `Connect AI tools to Hail via the Model Context Protocol (MCP) for inbound and outbound phone, SMS, and email`
- **Feature-claim honesty:** name only shipped providers — Twilio (voice + SMS) and AWS SES (email). Never imply Telnyx. "inbound and outbound" in the MCP line is a sanctioned positioning choice (see spec); keep it out of provider-breadth claims.
- **No auto-PR.** Push the branch; a human opens the pull request against `NangoHQ/nango`.
- **Commits:** Conventional Commits, no AI-attribution trailer.

---

### Task 1: Add the `hail` API provider entry

**Files:**

- Modify: `/Users/r/playground/nango/packages/providers/providers.yaml` (insert immediately before the `haileyhr:` entry)

**Interfaces:**

- Consumes: nothing (first task).
- Produces: a `hail` provider keyed for `auth_mode: API_KEY`; later tasks reference the slug `hail` and its docs URLs `https://nango.dev/docs/api-integrations/hail` and `.../hail/connect`.

- [ ] **Step 1: Confirm the working branch**

The controller has already created `feat/add-hail-provider` off `upstream/master` (the fork's `origin/master` was 139 commits stale). Just confirm you are on it and the tree is clean:

```bash
cd /Users/r/playground/nango
git branch --show-current   # expect: feat/add-hail-provider
git status -s               # expect: empty
```

If not on that branch, stop and report — do not create a new branch off the stale `origin/master`.

- [ ] **Step 2: Confirm the insertion anchor is still correct**

```bash
grep -nE "^(hackerrank-work|haileyhr|halo-psa):" packages/providers/providers.yaml
```

Expected: `hackerrank-work:` then `haileyhr:` then `halo-psa:`. Insert `hail:` immediately before `haileyhr:` (alphabetical: `hail` < `haileyhr`). If a real `hail:` entry already exists, stop and report a collision.

- [ ] **Step 3: Insert the `hail` block**

Place immediately before the `haileyhr:` line, at column 0 (top-level key), preserving the file's 4-space indentation:

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

- [ ] **Step 4: Run the schema validator — expect it to PASS**

```bash
npm run test:providers
```

Expected: exits 0, no error mentioning `hail`. (The validator checks the whole file; a schema violation in the new block — e.g. a stray `scope:` field or a mis-typed category — fails here with a message naming `hail`.)

- [ ] **Step 5: Behavioural check — the verification chain resolves**

This reproduces what Nango's `credentialsTest` does: GET the first verification endpoint with a real key and confirm 2xx; confirm unauthenticated is 401 (so the endpoint actually gates). Use a live `hl_live_` key supplied out-of-band; do not paste it into any file.

```bash
KEY='<hl_live_… supplied at run time>'
echo "authed /calls:"; curl -s -o /dev/null -w "%{http_code}\n" -H "Authorization: Bearer $KEY" https://api.hail.so/calls
echo "unauth /calls:"; curl -s -o /dev/null -w "%{http_code}\n" https://api.hail.so/calls
```

Expected: `authed /calls: 200` and `unauth /calls: 401`.

- [ ] **Step 6: Commit**

```bash
git add packages/providers/providers.yaml
git commit -m "feat(providers): add Hail API provider"
```

---

### Task 2: Add the `hail-mcp` provider entry

**Files:**

- Modify: `/Users/r/playground/nango/packages/providers/providers.yaml` (insert directly after the `hail:` block from Task 1, before `haileyhr:`)

**Interfaces:**

- Consumes: the `hail:` block precedes it (alphabetical: `hail` < `hail-mcp` < `haileyhr`).
- Produces: a `hail-mcp` provider keyed for `auth_mode: MCP_OAUTH2` with dynamic client registration; Task 3 references docs URL `https://nango.dev/docs/api-integrations/hail-mcp`.

**Why `registration_params` and `default_scopes` are load-bearing (do not drop):**

- Nango's DCR body (`packages/shared/lib/clients/mcp.client.ts:33-38`) sends `grant_types`/`response_types` **only** from `registration_params`. Without it, Hail registers the client with `authorization_code` only — no `refresh_token` — and the connection dies at the first token refresh (~30 days out), long after merge.
- Better Auth issues a refresh token only when `offline_access` is requested (`@better-auth/oauth-provider/dist/index.mjs:558`). `default_scopes: [offline_access]` supplies it. `scope:` is not a schema field.

- [ ] **Step 1: Insert the `hail-mcp` block**

Immediately after the `hail:` block's last line (`example: hl_live_************`) and before `haileyhr:`:

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

- [ ] **Step 2: Run the schema validator — expect it to PASS**

```bash
npm run test:providers
```

Expected: exits 0, no error mentioning `hail-mcp`. If it complains about an unknown `scope`/`registration_params`/`default_scopes` key, re-check against the schema at `scripts/validation/providers/schema.json` — `default_scopes` and `registration_params` are valid; `scope` is not.

- [ ] **Step 3: Behavioural check — the MCP protected resource points at Hail's AS**

```bash
curl -s https://mcp.hail.so/.well-known/oauth-protected-resource
```

Expected JSON containing `"authorization_servers":["https://hail.so/api/auth"]` and `"resource":"https://mcp.hail.so/"` (note the trailing slash — Nango echoes it as the `resource` param, and Hail's `validAudiences` accepts both forms).

- [ ] **Step 4: Behavioural check — DCR yields a refresh-capable client**

Reproduces Nango's registration call, then confirms authorize accepts `offline_access`. Creates one throwaway client row in Hail's prod `oauth_clients` (named `...(delete me)` for later cleanup):

```bash
CID=$(curl -s -X POST https://hail.so/api/auth/oauth2/register \
  -H "Content-Type: application/json" \
  -d '{"redirect_uris":["https://api.nango.dev/oauth/callback"],"token_endpoint_auth_method":"none","client_name":"Nango plan-verify (delete me)","grant_types":["authorization_code","refresh_token"],"response_types":["code"]}' \
  | python3 -c "import sys,json; d=json.load(sys.stdin); print(d['client_id']); import sys; print('grant_types:',d.get('grant_types'),file=sys.stderr)")
CH=$(python3 -c "import hashlib,base64;print(base64.urlsafe_b64encode(hashlib.sha256(b'a'*64).digest()).rstrip(b'=').decode())")
curl -s -o /dev/null -D - -G https://hail.so/api/auth/oauth2/authorize \
  --data-urlencode "client_id=$CID" \
  --data-urlencode "redirect_uri=https://api.nango.dev/oauth/callback" \
  --data-urlencode "response_type=code" \
  --data-urlencode "scope=offline_access" \
  --data-urlencode "state=probe" \
  --data-urlencode "resource=https://mcp.hail.so/" \
  --data-urlencode "code_challenge=$CH" --data-urlencode "code_challenge_method=S256" \
  | grep -i "^location:"
```

Expected: the DCR response's `grant_types` (on stderr) includes `refresh_token`; the authorize `location:` header points at `/signin?...` (accepted), **not** `error=invalid_scope`.

- [ ] **Step 5: Record the throwaway client for cleanup**

Append the `client_id` printed above to the follow-up note in the `hail` repo's `docs/submissions/nango.md` "Testing side effects" list (DCR returns no `registration_access_token`, so it can't self-delete). This is a note only — no code change.

- [ ] **Step 6: Commit**

```bash
git add packages/providers/providers.yaml
git commit -m "feat(providers): add Hail MCP provider"
```

---

### Task 3: Add docs pages and register them

**Files:**

- Create: `/Users/r/playground/nango/docs/api-integrations/hail.mdx`
- Create: `/Users/r/playground/nango/docs/api-integrations/hail/connect.mdx`
- Create: `/Users/r/playground/nango/docs/api-integrations/hail-mcp.mdx`
- Modify: `/Users/r/playground/nango/docs/docs.json` (register all three slugs — two page slugs: `hail` and `hail-mcp`; the `hail/connect` page is reached via `docs_connect` and is not separately listed, matching `resend`)

**Interfaces:**

- Consumes: provider slugs `hail` and `hail-mcp` from Tasks 1–2; their `docs`/`docs_connect` URLs must match the file paths created here.
- Produces: nothing downstream.

- [ ] **Step 1: Create `docs/api-integrations/hail.mdx`**

````mdx
---
title: "Hail"
sidebarTitle: "Hail"
description: "Integrate your application with the Hail API for phone, SMS, and email for AI agents and humans"
---

## 🚀 Quickstart

Connect to Hail with Nango and make your first API request in minutes.

<Steps>
    <Step title="Create the integration">
    In Nango ([free signup](https://app.nango.dev)), go to [Integrations](https://app.nango.dev/dev/integrations) -> _Configure New Integration_ -> _Hail_.
    </Step>
    <Step title="Authorize Hail">
    Go to [Connections](https://app.nango.dev/dev/connections) -> _Add Test Connection_ -> _Authorize_, then enter your API key (created at [hail.so/console](https://hail.so/console)). Later, you'll let your users do the same directly from your app.
    </Step>
    <Step title="Call the Hail API">
    Make your first request to the Hail API (list recent calls). Replace the placeholders with your [secret key](https://app.nango.dev/dev/environment-settings), [integration ID](https://app.nango.dev/dev/integrations), and [connection ID](https://app.nango.dev/dev/connections):

    ```bash
    curl "https://api.nango.dev/proxy/calls" \
      -H "Authorization: Bearer <NANGO-SECRET-KEY>" \
      -H "Provider-Config-Key: <INTEGRATION-ID>" \
      -H "Connection-Id: <CONNECTION-ID>"
    ```
    </Step>

</Steps>

## 📚 Hail Integration Guides

Nango-maintained guides for common use cases.

- [How to obtain your Hail API key](/api-integrations/hail/connect)
  Get your API key to connect Hail to Nango

Official docs: [Hail API Reference](https://hail.so/docs)
````

- [ ] **Step 2: Create `docs/api-integrations/hail/connect.mdx`**

Hail's docs are agent-first and avoid screenshots, so this mirrors `resend/connect.mdx`'s structure without the `<img>` tags:

```mdx
---
title: Hail - How do I link my account?
sidebarTitle: Hail
---

# Overview

To authenticate with Hail, you will need:

1. **API Key** – A key that grants secure access to the Hail API, letting authorized applications place calls and send SMS and email on your behalf.

This guide explains how to obtain your **API Key** from Hail.

### Prerequisites

- An active Hail account ([hail.so](https://hail.so))

### Instructions

#### Step 1: Getting your API key

1. Log in to your Hail account and open [the console](https://hail.so/console).
2. Create a new API key. Store it securely — it is only displayed once, and it is prefixed `hl_live_`.

#### Step 2: Enter credentials in the Connect UI

Once you have your **API Key**:

1. Open the form where you need to authenticate with Hail.
2. Enter your **API Key** in the API Key field.
3. Submit the form to complete authentication.

You are now connected to Hail.
```

- [ ] **Step 3: Create `docs/api-integrations/hail-mcp.mdx`**

````mdx
---
title: "Hail (MCP)"
sidebarTitle: "Hail (MCP)"
description: "Connect AI tools to Hail via the Model Context Protocol (MCP) for inbound and outbound phone, SMS, and email"
---

## 🚀 Quickstart

Connect to the Hail MCP server with Nango and call MCP tools in minutes. Hail MCP uses OAuth 2.1 with **dynamic client registration**, so no app registration is required.

<Steps>
    <Step title="Create the integration">
    In Nango ([free signup](https://app.nango.dev)), go to [Integrations](https://app.nango.dev/dev/integrations) -> _Configure New Integration_ -> _Hail (MCP)_.
    </Step>
    <Step title="Authorize Hail">
    Go to [Connections](https://app.nango.dev/dev/connections) -> _Add Test Connection_ -> _Authorize_, then log in to Hail. Later, you'll let your users do the same directly from your app.
    </Step>
    <Step title="Call a Hail MCP tool">
    Make your first MCP request (initialize handshake). Replace the placeholders with your [secret key](https://app.nango.dev/dev/environment-settings), [integration ID](https://app.nango.dev/dev/integrations), and [connection ID](https://app.nango.dev/dev/connections):

    ```bash
    curl "https://api.nango.dev/proxy/" \
      -X POST \
      -H "Authorization: Bearer <NANGO-SECRET-KEY>" \
      -H "Provider-Config-Key: <INTEGRATION-ID>" \
      -H "Connection-Id: <CONNECTION-ID>" \
      -H "Content-Type: application/json" \
      -H "Accept: application/json, text/event-stream" \
      -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"nango","version":"1.0"}}}'
    ```
    </Step>

</Steps>

Hail exposes tools to place calls and send SMS and email, plus read and tracking tools across all three channels.

Official docs: [Hail MCP setup](https://hail.so/mcp)
````

- [ ] **Step 4: Confirm the docs.json insertion anchor**

```bash
grep -n '"integrations/all/hackerrank-work"\|"api-integrations/haileyhr"' docs/docs.json
```

Expected: two consecutive lines, `hackerrank-work` directly above `haileyhr`. `hail`/`hail-mcp` go between them (`hail` and `hail-mcp` both sort before `haileyhr`).

- [ ] **Step 5: Insert the two page slugs into `docs.json`**

Between the `"integrations/all/hackerrank-work",` line and the `"api-integrations/haileyhr",` line, add:

```json
              "api-integrations/hail",
              "api-integrations/hail-mcp",
```

(Match the surrounding indentation exactly. Do not list `hail/connect` — it is reached via `docs_connect`, mirroring `resend`.)

- [ ] **Step 6: Verify `docs.json` is still valid JSON and entries are present**

```bash
python3 -c "import json; d=json.load(open('docs/docs.json')); print('valid json')"
grep -n '"api-integrations/hail"\|"api-integrations/hail-mcp"' docs/docs.json
```

Expected: `valid json`, then the two new lines printed. A trailing-comma or brace error fails the first command.

- [ ] **Step 7: Commit**

```bash
git add docs/api-integrations/hail.mdx docs/api-integrations/hail/connect.mdx docs/api-integrations/hail-mcp.mdx docs/docs.json
git commit -m "docs: add Hail and Hail (MCP) integration pages"
```

---

### Task 4: Live end-to-end test via local Docker Compose

This is the acceptance gate the CONTRIBUTING guide requires ("please thoroughly test the integration"). It is manual and needs a browser and a human — schema validation alone proves neither the proxy nor the OAuth flow. No commit; this task gates the PR.

**Files:** none (verification only).

**Interfaces:**

- Consumes: the committed `providers.yaml` and docs from Tasks 1–3.

- [ ] **Step 1: Start Nango locally**

```bash
cd /Users/r/playground/nango
docker compose up
```

Expected: stack boots; local UI reachable at http://localhost:3003. (Adjust ports in `docker-compose.yaml` if they clash.)

- [ ] **Step 2: Test the `hail` API connection**

In the UI at http://localhost:3003: Configure New Integration → **Hail** → create it. Then Connections → Add Test Connection → paste a live `hl_live_` key → Authorize.
Expected: the connection appears in _Connections_ with valid credentials (Nango ran the `/calls` verification and it returned 2xx).

- [ ] **Step 3: Test the `hail-mcp` OAuth connection**

Configure New Integration → **Hail (MCP)** → create it (dynamic registration needs no client ID). Then Connections → Add Test Connection → Authorize → complete the Hail login + consent in the browser.
Expected: connection created and shown as valid.

- [ ] **Step 4: Confirm a refresh token was actually issued (the one unverified claim)**

In the connection's detail view, confirm a **refresh token** is present alongside the access token. (This is the single item probing could not confirm ahead of time; if it is absent, `registration_params.grant_types` or `default_scopes` is wrong — revisit Task 2.)
Expected: refresh token present.

- [ ] **Step 5: Record results**

Note pass/fail for each of Steps 2–4 in `hail` repo → `docs/submissions/nango.md` under Notes, and flip the `- [ ]` "Confirm a refresh_token actually comes back" TODO there to done if Step 4 passed. Note only — no nango-repo change.

---

### Task 5: Push branch and hand off for PR

**Files:** none.

- [ ] **Step 1: Review the full diff**

```bash
cd /Users/r/playground/nango
git log --oneline upstream/master..HEAD
git diff upstream/master..HEAD --stat
```

Expected: three commits (two providers, one docs); changed files limited to `packages/providers/providers.yaml`, the three `docs/api-integrations/hail*` files, and `docs/docs.json`.

- [ ] **Step 2: Final validator run on the branch tip**

```bash
npm run test:providers && python3 -c "import json; json.load(open('docs/docs.json')); print('docs.json ok')"
```

Expected: validator exits 0; `docs.json ok`.

- [ ] **Step 3: Push the branch**

```bash
git push -u origin feat/add-hail-provider
```

- [ ] **Step 4: Hand off — do NOT open the PR**

Report the pushed branch to the user with a suggested PR title `Add Hail` against `NangoHQ/nango:master`, and the PR-description tagline from `docs/submissions/nango.md`:

> Hail is phone, SMS, and email for AI agents and humans — inbound and outbound. One MCP, CLI, and API for talking to the real world.

The human opens the PR (per the never-auto-PR constraint). Reference merged examples: https://github.com/NangoHQ/nango/pulls?q=is%3Apr+is%3Amerged+label%3Aapi

---

## Notes for the implementer

- **Line numbers drift.** Every anchor here was captured against `upstream/master` (NangoHQ/nango) on 2026-07-16, the base of `feat/add-hail-provider`. Re-grep before each edit; trust the grep, not the number.
- **Don't touch the `hail` repo from the nango branch.** Spec and submission-tracker updates (cleanup client IDs, test results) are notes in the `hail` repo, made separately — they never enter the Nango PR.
- **If the validator rejects a category or field,** the schema at `scripts/validation/providers/schema.json` is ground truth; the design spec's field list was derived from it, but re-check rather than assume.
