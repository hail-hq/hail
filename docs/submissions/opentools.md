---
target: "OpenTools"
slug: opentools
category: mcp-registry
url: "https://opentools.com/registry"
score: 3.8
status: drafted
---

# OpenTools

## TODO

- [ ] Confirm there is no self-serve submission form anywhere on opentools.com before booking a call — re-check `/registry` and any footer/nav "submit" or "list your server" link in case that's changed
- [ ] Find the actual scheduling link/contact (no public booking URL located during drafting — check site footer, `/about`, `/contact`, and any listed team email or Calendly/Cal.com link)
- [ ] Draft copy reviewed against the feature-claim policy (voice/email present-tense and vendor-accurate; SMS flagged as not yet shipped) — done below
- [ ] Logo asset ready — `hail-website/public/assets/hail-monogram.svg` (square mark) or `avatar-1024.png` (raster) in case the call requires an emailed asset in advance
- [ ] Call scheduled
- [ ] Call held — capture whatever undocumented requirements/criteria they state, and record here
- [ ] Submitted (however the call determines — form, email follow-up, etc.)
- [ ] Confirmed live — record listing URL in Notes

## Steps to submit

1. Go to [opentools.com/registry](https://opentools.com/registry) and look for a "list your server" / "submit" / "get listed" link (nav, footer, or an empty-state prompt on the registry page itself). If one exists now, this file is stale — use it and skip the call.
2. If no self-serve path is present (the case as of this draft), look for a contact channel on the same domain: footer email address, `/contact` page, or a scheduling-tool link (Calendly, Cal.com, Savvycal). OpenTools does not publish a documented submission process, so this step requires live reconnaissance at submission time.
3. Book the call using whatever mechanism is found. When requesting/confirming the slot, mention up front that this is an MCP server registry listing request for "Hail" — an open-source communication platform for AI agents — so the team can route it correctly.
4. Before the call, have ready: the one-liner, description, server URL, tags, and license from **Content** below, plus the logo asset path. Paste/attach whatever the team asks for during or after the call — since requirements are undocumented and decided case-by-case, don't assume a fixed field set; follow their lead.
5. During the call, ask directly: what they need from us (README, live server URL, demo, screenshots), what their review/acceptance criteria are, and expected turnaround to go live.
6. After the call, send any follow-up materials they requested by email, using the same copy in **Content** below so wording stays consistent with our other registry listings.
7. Once confirmed live, update this file's frontmatter `status` to `submitted` (or `submitted (live)` per the tracker convention) and add the listing URL plus who we talked to in **Notes**.

## Content

**Name:** Hail

**One-liner:** Phone, SMS & email — for agents.

**Description:**
Hail is a self-hostable, AGPLv3 communication platform built for AI agents. It gives an agent a real phone number and inbox — place and receive calls, send and receive email, and read back structured delivery events — all through one remote MCP server. No local install: point a client at `https://mcp.hail.so`, authorize once via OAuth, and the agent gets tools immediately. Self-hosters run the same server with a static API key instead of OAuth. Also available via CLI, a Python SDK, and OpenAPI for non-MCP integrations.

**Server URL:** `https://mcp.hail.so` (Hail Cloud) / `http://<your-host>:8081` (self-hosted)

**Transport:** Remote, Streamable HTTP — no stdio, no local install (see [docs/setup/mcp.md](../setup/mcp.md#why-remote-only-no-stdio--no-pypi-install))

**Language:** Python

**Repo:** `https://github.com/hail-hq/hail`

**License:** AGPL-3.0-or-later — [`LICENSE`](../../LICENSE)

**Tags/keywords:** communication, voice, phone, sms, email, agents, self-hosted, open-source

**Tools exposed** (per `mcp/hailhq/mcp/tools.py`):
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

**Logo asset:** `hail-website/public/assets/hail-monogram.svg` (square mark; `avatar-1024.png` as a raster fallback)

## Notes

- OpenTools has no documented self-serve submission form for the registry — listing requires scheduling a call with the team, and their acceptance requirements are not published; treat whatever they state on the call as authoritative and record it here afterward for future re-submissions.
- No scheduling link or contact address was located during drafting. This needs live discovery at submission time — check the site's footer, an `/about` or `/contact` page, and any embedded scheduler.
- **SMS is roadmap, not shipped — don't let any verbal or written follow-up imply otherwise.** `core/hailhq/core/providers/` has `voice/` and `email/` subpackages but no `sms/` — no SMS provider is wired to any carrier. The README's own capability checklist confirms this: under "SMS," both Outbound → Twilio and Inbound → Twilio are unchecked. There is no `hail sms` CLI command, no SMS route in the OpenAPI spec, and no `send_sms`/`list_sms` MCP tool. The one-liner "Phone, SMS & email" mirrors Hail's own README tagline (brand voice, product-level pitch) — if asked to elaborate live on the call, say SMS is "coming soon," not shipped.
- Voice is Twilio-backed (`core/hailhq/core/providers/voice/twilio.py`); email is AWS SES-backed (`core/hailhq/core/providers/email/ses.py`). No other carrier/vendor is wired up yet — don't name any other provider (e.g. Telnyx, an unchecked README roadmap item) as already integrated.
- No review turnaround time is published; expect it to depend on the outcome of the call. Update this section with actual turnaround once experienced.
