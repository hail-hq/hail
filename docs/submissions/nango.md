---
target: "Nango (NangoHQ/nango providers.yaml)"
slug: nango
category: github-list
url: "https://github.com/NangoHQ/nango"
score: 6.2
status: drafted
---

# Nango (NangoHQ/nango providers.yaml)

Unlike every other target in this tracker, this is a **code contribution**, not a
listing. Nango's connector catalogue is a committed `providers.yaml`; being
"listed" means a provider block plus docs pages merged into their repo. Design
and evidence: [`../superpowers/specs/2026-07-15-nango-connector-design.md`](../superpowers/specs/2026-07-15-nango-connector-design.md).

## TODO

- [ ] No account needed beyond a free GitHub account (fork-based PR).
- [ ] Two entries, not one: `hail` (API_KEY) and `hail-mcp` (MCP_OAUTH2). Nango has
      no "type" field — one entry per auth surface, per `slack`/`slack-mcp` precedent.
- [ ] **Do not describe inbound phone calls as shipped.** Verified against
      `README.md` Milestones and `core/hailhq/core/providers/`: `voice/twilio.py`
      is outbound-only; inbound Twilio is unchecked. SMS (in + out) and email
      (in + out) _are_ shipped — `providers/sms/twilio.py` and
      `providers/email/{ses.py,inbound/ses.py}` all exist.
- [ ] **Provider breadth stays honest:** Twilio (voice + SMS) and AWS SES (email)
      only. Telnyx is unchecked in Milestones — never imply it.
- [ ] Run `npm run test:providers` in the nango checkout; must pass before PR.
- [ ] Test both integrations live via Nango's local Docker Compose (their
      contributing guide requires real testing, and schema validation proves
      neither the proxy nor the OAuth flow).
- [ ] **Confirm a `refresh_token` actually comes back** from the `hail-mcp` OAuth
      flow. Not yet verified — probing stops at the login page, which needs a
      human browser. This is the single highest-risk unknown.
- [ ] Open PR (human action — never auto-opened).
- [ ] Address maintainer review.
- [ ] Confirm merged and live at `nango.dev/docs/api-integrations/hail`.

## Steps to submit

1. Work in the existing fork `r13i/nango` (`/Users/r/playground/nango`, currently
   `master`). Branch off per gitflow, e.g. `feat/add-hail-provider`.
2. Add both blocks from **Content** to `packages/providers/providers.yaml`,
   placed **alphabetically** (after `gusto`).
3. Write the three docs pages (see Content):
   `docs/api-integrations/hail.mdx`, `docs/api-integrations/hail/connect.mdx`,
   `docs/api-integrations/hail-mcp.mdx`.
4. Register all three in `docs/docs.json`, alphabetically, as
   `api-integrations/<slug>`. (The `integrations/all/<slug>` form is legacy —
   some entries still use it; new ones should not.)
5. Validate: `npm run test:providers`
   (`scripts/validation/providers/validate.ts`).
6. Spin up Nango locally via Docker Compose. Create both integrations in the UI
   and establish a real connection for each:
   - `hail`: paste a live `hl_live_` key; verification must pass.
   - `hail-mcp`: complete the browser OAuth flow; confirm a refresh token is
     returned.
7. Push the branch. **A human opens the PR** against `NangoHQ/nango:master` —
   per the registry-submissions non-goal on auto-opening third-party PRs.
8. Alternative low-effort route: post in `#request-a-new-api` on Nango's
   community Slack and let their team implement it to their SLA. Zero control
   over timing or content; only worth it if the PR stalls.
9. Once merged, confirm the docs pages render at
   `https://nango.dev/docs/api-integrations/hail` and `.../hail-mcp`.

## Content

File: `packages/providers/providers.yaml` (alphabetical, after `gusto`)

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

Docs frontmatter — the **only** prose Nango asks for. `providers.yaml` has no
`description` field; these two strings are the entire copy surface.

`docs/api-integrations/hail.mdx`:

```yaml
---
title: "Hail"
sidebarTitle: "Hail"
description: "Integrate your application with the Hail API for phone, SMS, and email for AI agents and humans"
---
```

`docs/api-integrations/hail-mcp.mdx`:

```yaml
---
title: "Hail (MCP)"
sidebarTitle: "Hail (MCP)"
description: "Connect AI tools to Hail via the Model Context Protocol (MCP) for inbound and outbound phone, SMS, and email"
---
```

`connect.mdx` needs no description (`resend/connect.mdx` follows a different
structure).

PR description (the tagline has no home in the docs page — Nango's body is fully
templated: Quickstart, Integration Guides, Pre-built syncs, with no blurb slot):

> Hail is phone, SMS, and email for AI agents and humans — inbound and outbound.
> One MCP, CLI, and API for talking to the real world.

Copy notes:

- The API description keeps the house formula — 346 of 364 pages are exactly
  `Integrate your application with the <X> API` — plus a trailing `for <purpose>`
  clause, the one proven variant (`a-leads`, `dialpad-wfm`, `jamie`,
  `surecontact`). `resend` and `slack` are the live peers; `twilio.mdx` and
  `sendgrid.mdx` are still on the legacy `integrations/all/` path.
- The MCP description follows the `linear-mcp` / `notion-mcp` / `superhuman-mcp`
  shape. "AI agents and humans" is dropped there as redundant — the line already
  opens with "Connect AI tools". Tool surface behind it:
  `mcp/hailhq/mcp/tools.py` (15 tools: `place_call`, `send_sms`, `send_email`,
  plus 12 read/track tools).
- Conventions verified across all 364 pages: `&` appears 0 times (use "and"),
  em-dash 0 times, `API to <verb>` 0 times, and "AI agent"/"humans" 0 times —
  Hail is the first to say it. Longest existing description is 141 chars; both of
  these fit under that.
- No trailing period: 0 of 364 descriptions end with one. The API line carries a
  double "for"; alternative if the comma reads better is
  `'...for phone, SMS, and email, for AI agents and humans'`. A colon or dash has
  no precedent — don't reach for one.
- The docs descriptions stay directionless on channels; **the "inbound and
  outbound" positioning lives only in the PR-description tagline above** (user
  decision, 2026-07-16). Inbound phone calls are **not shipped** —
  `voice/twilio.py` is outbound-only and Milestones has inbound Twilio unchecked;
  SMS and email are genuinely two-way. The tagline claim is sanctioned by the
  feature-claim policy in `2026-07-06-registry-submissions-design.md`: core
  capabilities are written present-tense "regardless of today's milestone checkbox
  state", and only provider breadth stays honest to current state (hence
  Twilio/SES named, Telnyx never). Revisit if inbound voice slips.

Field notes:

- `categories`: three, plus `mcp` on the MCP entry. Evidence from all 872
  categorised providers: 583 use exactly one category, 232 use two, only 3 use
  five, none use six. `popular` is Nango-curated and never self-assigned.
  `other` is excluded — it is a catch-all that only 3 of its 45 users pair with
  a real category, and `communication` already applies. `marketing` is carried
  for the email surface (`/email-domains`, `/sms/suppressions`, `/unsubscribe`),
  the same reason `sendgrid` uses it.
- `base_url`: hardcoded to Hail Cloud. 72 providers template `base_url` from
  `connection_config` for self-hosting, but that forces every Cloud user to type
  a hostname. If self-host demand appears, add a separate `hail-self-hosted`
  entry rather than adding friction here.
- `verification.endpoints`: a **fallback chain**, not a coverage list —
  `credentialsTest` (`packages/server/lib/hooks/hooks.ts:468`) returns on the
  first 2xx. Listing three is insurance for when scope enforcement ships
  (`mcp/hailhq/mcp/server.py:54`, "deferred to Phase 2"); today no route
  enforces scopes, so `/calls` always answers.
- `registration_params`: **mandatory.** Nango's DCR body
  (`packages/shared/lib/clients/mcp.client.ts:33-38`) omits `grant_types`
  unless `registration_params` supplies it, and Hail then registers the client
  with `authorization_code` only — no `refresh_token`. Verified against live
  DCR. Without this the connection works for 30 days, then breaks permanently.
- `default_scopes`: `offline_access` is what makes Better Auth issue a refresh
  token (`@better-auth/oauth-provider/dist/index.mjs:558`). `scope:` is **not**
  a schema field (`additionalProperties: false`) — 51 providers use
  `default_scopes`, 0 use `scope`.
- No `authorization_params: response_type: code` — `response_type` is in
  `reservedOAuthKeys` (`oauth.controller.ts:944`) and filtered before use. It is
  dead config; `linear-mcp` carries it anyway.
- Assets, if the PR description wants one:
  `/Users/r/playground/hail-website/public/assets/og-card-1200x630.png` or
  `hail-wordmark.svg`.

## Notes

- **Category fit is imperfect.** `github-list` is the closest existing value
  (fork → PR → merged into a catalogue in a GitHub repo), but Nango is an
  integration platform, not an awesome-list. It matches none of the Phase 1
  categories in `2026-07-06-registry-submissions-design.md` exactly. Rename if a
  better value emerges.
- **Score 6.2 is an estimate, not a measurement.** Reasoning: durable payoff (a
  permanent catalogue entry plus two SEO'd docs pages on nango.dev), highly
  targeted audience, and low rejection risk since it's tested code rather than
  marketing copy — offset by high effort (YAML + 3 docs pages + local Docker
  testing + review cycle) and partial ICP overlap, since Nango's users wire SaaS
  integrations and may not need agent voice calls. Adjust freely.
- **Capability claims verified 2026-07-15** against `README.md` Milestones and
  `core/hailhq/core/providers/`: outbound calls (Twilio) shipped; **inbound
  calls not shipped**; SMS outbound + inbound (Twilio) shipped; email outbound +
  inbound (AWS SES, custom sender domains) shipped. Note this supersedes
  `awesome-selfhosted-awesome-selfhosted-data.md` (drafted 2026-07-07), which
  correctly said SMS was unshipped at the time — `providers/sms/twilio.py` has
  since landed. **That older draft's description is now stale and understates
  Hail; re-check it before submitting.**
- **Testing side effects (2026-07-15):** live DCR probing created 4 junk rows in
  the production `oauth_clients` table, all named `...(delete me)`:
  `QxFRuVYz…`, `qKUWIwUs…`, `rpWTAOgH…`, `yoOFiAwN…`. DCR returns no
  `registration_access_token`, so RFC 7592 deletion is unavailable — remove them
  directly. Tracked as a follow-up in the design spec.
- **Review turnaround:** unknown. Nango publishes SLAs for the
  `#request-a-new-api` route but not for community PRs.
- **Contact used:** none yet — not filed.
- **Roadmap items deliberately excluded from copy:** inbound phone calls,
  Telnyx, Whisper/AssemblyAI STT, Deepgram Aura TTS.
