---
target: "mcp.so"
slug: mcp-so
category: mcp-registry
url: "https://mcp.so/submit"
score: 6.5
status: drafted
---

# mcp.so

## TODO

- [x] Confirm `https://mcp.hail.so` is publicly reachable over HTTPS (Hail Cloud's standing MCP endpoint) — re-verified 2026-07-07.
- [x] Confirm `https://github.com/hail-hq/hail` is public — the repo link doubles as this submission's required "public URL/repo".
- [x] **Corrected 2026-07-07: the real form is far simpler than originally drafted.** Fetched `https://mcp.so/submit` directly (server-rendered, not JS-blocked) — it has exactly **two fields**: `name` (text) and `url` (must be a GitHub repo URL, per its own placeholder example `https://github.com/chatmcp/mcp-server-github`). No description, category, tags, or logo fields exist on this form — mcp.so evidently auto-indexes the rest from the repo, similar to Glama. No account/login visible on the page.
- [ ] No prep needed beyond the two values below — this is now a ~10-second manual fill, no draft copy/asset decisions required.
- [ ] Submitted via mcp.so/submit.
- [ ] Confirmed live — record the listing URL in Notes.

## Steps to submit

**Corrected 2026-07-07 — simpler than originally drafted:**

1. Go to [mcp.so/submit](https://mcp.so/submit).
2. Fill in **Name**: `Hail`
3. Fill in **URL**: `https://github.com/hail-hq/hail`
4. Submit. That's the entire form — no other fields exist.
5. Once mcp.so publishes the listing, update this file's frontmatter `status` to `submitted`, and once confirmed visible on the live site, add the listing URL to Notes.

## Content

**Name:** Hail

**One-liner (short description):** Phone, SMS & email — for agents.

**Description (long):**
Hail is a self-hostable, AGPLv3 communication platform built for AI agents. It gives an agent a real phone number, inbox, and messaging line — place and receive calls, send and receive email, and read back structured events — all through one remote MCP server. No local install: point a client at `https://mcp.hail.so`, authorize once via OAuth, and the agent has tools immediately. Self-hosters run the same server with a static API key instead of OAuth. Also available via CLI, a Python SDK, and OpenAPI for non-MCP integrations.

**Link:** `https://github.com/hail-hq/hail`

**Category:** mcp-registry

**Server URL (if a separate field exists):** `https://mcp.hail.so`

**Tags/keywords (if the form supports them):** communication, voice, phone, sms, email, agents, self-hosted, open-source

**License:** AGPLv3

**Logo asset:** `hail-website/public/assets/hail-monogram.svg` (square mark; `hail-website/public/assets/avatar-1024.png` as raster fallback)

## Notes

- mcp.so's stated submission mechanism is a plain web form (name, description, link, category) — no OAuth-detection scan like Smithery's, so there's nothing to verify around a `401`/`WWW-Authenticate` challenge for this listing. No stated review SLA or contact channel found for mcp.so; treat it as self-serve via the public form.
- The MCP tool surface today is eleven tools, all call/email — `place_call`, `send_email`, `get_call`, `list_calls`, `get_email`, `list_emails`, `get_email_raw`, `get_email_attachment`, `get_email_events`, `get_email_stats`, `get_events` (verified against `mcp/hailhq/mcp/tools.py` in the `hail` repo). There is no `send_sms`/`list_sms` tool wired in yet — SMS-over-MCP is "coming soon." SMS is fine to mention as a shipped Hail _product_ capability in the general description, but don't imply it's callable as an MCP tool today.
- Provider wiring verified in `core/hailhq/core/providers/`: voice is Twilio-backed (`voice/twilio.py`), outbound email is SES-backed (`email/ses.py`), inbound email is SES-backed (`email/inbound/ses.py`). An `SmtpInboundProvider` class exists (`email/inbound/smtp.py`) but every method raises `NotImplementedError` — it's a placeholder for a future cloud-agnostic milestone, not a wired path; don't claim SMTP inbound, list it as "coming soon" if mentioned at all. No other carrier/vendor is wired up — don't name any other provider in listing copy.
- Repo link (`github.com/hail-hq/hail`) doubles as the "public URL/repo" requirement; `https://mcp.hail.so` is the live endpoint to use if the form asks for one separately.
