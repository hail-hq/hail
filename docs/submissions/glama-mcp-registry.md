---
target: "Glama MCP Registry"
slug: glama-mcp-registry
category: mcp-registry
url: "https://glama.ai/mcp/servers"
score: 6.8
status: n/a
---

# Glama MCP Registry

> **Abandoned 2026-07-07.** The `/mcp/servers` listing's Build & Release check requires wrapping the server with `mcp-proxy` as a stdio child process — structurally incompatible with Hail's deliberately remote-only HTTP MCP server (confirmed via direct reproduction: `mcp-proxy` waits 60s for a stdio JSON-RPC handshake that a Streamable HTTP server never sends). The `/mcp/connectors` route (verify via `/.well-known/glama.json`, shipped in PR #5 + #6) was the correct alternative, but this is no longer being pursued — deprioritized in favor of higher-value remaining targets. `glama.json` (PR #4) and the `/.well-known/glama.json` route (PR #5, #6) are harmless, already-merged additions; no need to revert them.

## TODO

- [x] Confirm `github.com/hail-hq/hail` is public (the repo URL _is_ the submission payload — Glama indexes from it directly)
- [x] Confirm root `README.md` reads cleanly as a standalone doc for a crawler: install (`git clone` + `docker compose up`), usage (CLI/HTTP/MCP snippets), license — all present as of this draft (verified below)
- [x] ~~Confirm live whether glama.ai/mcp/servers/new requires GitHub sign-in or accepts an anonymous URL paste~~ — **corrected 2026-07-07: no submission step exists at all.** Glama auto-discovers MCP servers from GitHub; the listing was already live at `https://glama.ai/mcp/servers/hail-hq/hail` with no action taken. See Notes.
- [x] Draft copy reviewed against the feature-claim policy (voice/email present-tense and vendor-accurate; SMS flagged as not yet shipped)
- [ ] Logo asset ready — `hail-website/public/assets/hail-monogram.svg` (square mark, light) or `avatar-1024.png` (raster) — only relevant if the claimed dashboard offers an editable avatar; unconfirmed until claim completes
- [x] "Claim" the org-owned listing via `glama.json` + "Login with GitHub to claim" — `glama.json` added and PR opened: https://github.com/hail-hq/hail/pull/4
- [ ] Once claimed: configure Docker build instructions to point at `mcp/Dockerfile` (repo-root build context — the file does `COPY core /app/core` + `COPY mcp /app/mcp`, so it is NOT self-contained if built from within `mcp/`) — this is likely why the auto-crawl build/introspection check is stuck pending
- [ ] Once a real score renders (not "pending"), pull the badge from `https://glama.ai/mcp/servers/hail-hq/hail/badges/score.svg` and add it to the already-open `awesome-mcp-servers` PR (#9561) per that repo's `glama-check` bot requirement
- [ ] Confirmed live — record final listing URL/score in Notes

## Steps to submit

**Corrected 2026-07-07 — there is no submission step.** Glama auto-discovers MCP servers straight from GitHub; the listing at `https://glama.ai/mcp/servers/hail-hq/hail` was already live with zero action taken. The real remaining work is _claiming_ the listing (org-owned repos require this) and getting the automated Docker build check to pass:

1. Merge (or at minimum push) https://github.com/hail-hq/hail/pull/4, which adds `glama.json` (`{"maintainers": ["r13i"]}`) to the repo root — required before claiming an org-owned listing.
2. Go to [glama.ai/mcp/servers/hail-hq/hail](https://glama.ai/mcp/servers/hail-hq/hail) and click **"Login with GitHub to claim"**.
3. Once claimed, open the Docker build configuration and point it at `mcp/Dockerfile`. Note: that Dockerfile does `COPY core /app/core` and `COPY mcp /app/mcp`, so it is only buildable with the **repo root** as build context (`docker build -f mcp/Dockerfile .`), not from inside `mcp/`. If Glama's config only accepts a Dockerfile path (not a separate context field), this may still fail — flag to Glama support/Discord if so.
4. Wait for their check to pass ("we only need the server to start and respond to introspection requests," per their bot). Confirm by reloading the listing page and checking the badge SVG at `https://glama.ai/mcp/servers/hail-hq/hail/badges/score.svg` renders a real score, not a placeholder.
5. Once scored, copy the badge markdown (`[![hail-hq/hail MCP server](https://glama.ai/mcp/servers/hail-hq/hail/badges/score.svg)](https://glama.ai/mcp/servers/hail-hq/hail)`) into the already-open `awesome-mcp-servers` PR #9561 — that repo's `glama-check` bot requires it before merge.
6. Update this file and the README index once both PRs (#4 here, #9561 upstream) are merged and the badge is live.
7. Once live, update this file's frontmatter `status` to `submitted` and add the listing URL to **Notes**.

## Content

**Repo URL (the actual submission payload):** `https://github.com/hail-hq/hail`

**Name:** Hail

**One-liner:** Phone, SMS & email — for agents.

**Description (if an editable field is offered post-index):**
Hail is a self-hostable, AGPLv3 communication platform built for AI agents. It gives an agent a real phone number and inbox — place and receive calls, send and receive email, and read back structured delivery events — all through one remote MCP server. No local install: point a client at `https://mcp.hail.so`, authorize once via OAuth, and the agent gets tools immediately. Self-hosters run the same server with a static API key instead of OAuth. Also available via CLI, a Python SDK, and OpenAPI for non-MCP integrations.

**Hosting type (Glama facet):** Remote (Streamable HTTP; no stdio, no local install — see [docs/setup/mcp.md](../setup/mcp.md#why-remote-only-no-stdio--no-pypi-install))

**Language (Glama facet):** Python

**Server URL:** `https://mcp.hail.so` (Hail Cloud) / `http://<your-host>:8081` (self-hosted)

**Tags/keywords (if offered):** communication, voice, phone, sms, email, agents, self-hosted, open-source

**Tools exposed (per `mcp/hailhq/mcp/tools.py`, source of truth — for reference if auto-index misses any):**
| Tool | Does |
|---|---|
| `place_call` | Originate an outbound phone call |
| `send_email` | Send an outbound email |
| `get_call` | Fetch the current state of one call |
| `list_calls` | List recent calls (cursor-paginated) |
| `get_email` | Fetch one email's full record (body + inbound headers) |
| `list_emails` | List emails (`direction="inbound"` for replies) |
| `get_email_raw` | Presigned URL for an inbound email's raw MIME |
| `get_email_attachment` | Presigned URL for one inbound attachment |
| `get_email_events` | Page through an email's delivery/event history |
| `get_email_stats` | Aggregate email delivery/deliverability stats |
| `get_events` | Page through the account-wide event stream |

**License:** AGPL-3.0-or-later — [`LICENSE`](../../LICENSE)

**Logo asset (if a separate avatar upload exists):** `hail-website/public/assets/hail-monogram.svg` (square mark; `avatar-1024.png` as a raster fallback)

## Notes

- Glama's stated mechanism is "submit public repo URL, auto-indexes tools" — there's no long-form listing form like Smithery/mcp.so; the repo's own README and LICENSE _are_ the submission. Keep `README.md` accurate rather than polishing a separate description.
- Hail's MCP server is remote-only (Streamable HTTP, OAuth on Cloud / static key self-hosted) with no stdio entry point and no `npx`/`uvx`-style local run command. Glama's auto-indexer is built primarily around repos that declare a local server command; if it can't find one here, expect the listing to end up thinner (README text, license, repo metadata) rather than a live enumerated tool list. That's a limitation of the target, not something to fix in this repo.
- **SMS is roadmap, not shipped — don't let any editable field imply otherwise.** `core/hailhq/core/providers/` has `voice/` and `email/` subpackages but no `sms/` — there is no SMS provider wired to any carrier at all (not even Twilio, which is already integrated for voice). The README's own capability checklist confirms this: under "SMS," both Outbound → Twilio and Inbound → Twilio are unchecked. There is no `hail sms` CLI command, no SMS route in the OpenAPI spec, and no `send_sms`/`list_sms` MCP tool. The one-liner "Phone, SMS & email" mirrors Hail's own README tagline (brand voice, product-level pitch), but if Glama's editable description field lets it stand alone without that context, add "(SMS: coming soon)" rather than implying a working SMS tool exists today.
- Voice is Twilio-backed (`core/hailhq/core/providers/voice/twilio.py`); email is AWS SES-backed (`core/hailhq/core/providers/email/ses.py`). No other carrier/vendor is wired up yet — don't name any other provider (e.g. Telnyx, which appears only as an unchecked README roadmap item) in listing copy.
- Couldn't fully verify Glama's live submission form mechanics — glama.ai/mcp/servers is a JS-rendered SPA; a fetch of both `/mcp/servers` and `/mcp/servers/new` returned only the page shell (nav shows an "Add Server" link and a "Sign Up" option; category/hosting-type/language facets are visible in the shell, but no submission form fields render without JS). Confirm the sign-in requirement and any editable fields at submission time — flagged in TODO above.
- No documented review turnaround or contact channel found; treat as self-serve/automated until proven otherwise.
- **2026-07-07 update — everything above about "submission form mechanics" was moot.** Confirmed live: Glama had already auto-indexed `hail-hq/hail` with zero action from us (`https://glama.ai/mcp/servers/hail-hq/hail` returns 200, titled "hail by hail-hq | Glama"). The badge endpoint also resolves regardless of claim/score status. The real blocker discovered via the `awesome-mcp-servers` PR's `glama-check` bot: unclaimed org-owned listings can't have Docker build instructions configured, and Glama's auto-crawl apparently expects a root-level Dockerfile — ours is nested at `mcp/Dockerfile`, likely why the introspection check never ran. Claim flow needs `glama.json` (org repos only) + "Login with GitHub to claim" — PR open at https://github.com/hail-hq/hail/pull/4, branched from `origin/main` (`9813ace`) in an isolated worktree to avoid bundling ~12 unrelated unpushed local commits from concurrent work.
- Glama's page is a JS-rendered SPA server-side (curl only returns the shell/title, no score/status data) — check the live score by opening the listing URL in an actual browser, not by fetching it.
