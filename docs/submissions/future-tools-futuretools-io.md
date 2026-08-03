---
target: "Future Tools (futuretools.io)"
slug: future-tools-futuretools-io
category: ai-directory
url: "https://futuretools.io/submit-a-tool"
score: 5.8
status: drafted
---

# Future Tools (futuretools.io)

## TODO

- [ ] Confirm the "Your Name" value to submit as (founder's display name)
- [ ] Decide whether to opt into the Future Tools newsletter checkbox (default recommendation below: leave unchecked)
- [ ] Reconfirm the live Category dropdown options at submission time (recorded list may drift) and pick the closest match
- [ ] Reconfirm the live Pricing dropdown options and pick the closest match (recorded as Free / Freemium / Paid / Open Source)
- [ ] Draft copy reviewed against the feature-claim policy (SMS capability claim vs. no SMS provider/tool wired up yet — see Notes)
- [ ] CAPTCHA solved and form submitted by a human (not automatable)
- [ ] Confirmed live — record the listing URL in Notes

## Steps to submit

1. Go to [futuretools.io/submit-a-tool](https://futuretools.io/submit-a-tool). No account or login is required — this is a single free-form submission page.
2. In **Tool Name**, enter: `Hail`
3. In **Tool URL**, enter: `https://hail.so`
4. In **Short Description**, paste the description from **Content** below (the primary short version; if the field visibly allows more characters, use the expanded version instead).
5. In the **Category** dropdown, select **Productivity** (closest available match — there is no "Developer Tools" or "Communication" option; **Other** is the fallback if Productivity feels wrong on the day).
6. In the **Pricing** dropdown, select **Freemium** (self-hosting is free under AGPLv3; the hosted Hail Cloud service is paid, usage-based — see Content for the exact framing).
7. Fill in **Your Name** and **Your Email** with the submitter's real name and `redouane.a.achouri@gmail.com`.
8. Leave the newsletter opt-in checkbox unchecked unless the submitter wants to receive Future Tools' newsletter.
9. Solve the CAPTCHA.
10. Click submit.
11. There is no stated review SLA — the page only says a person named Matt reviews submissions manually. Check back periodically for the listing to appear; once it's live, update this file's frontmatter `status` to `submitted` (then to `submitted (live)`) and record the listing URL in **Notes**.

## Content

**Tool Name:** Hail

**Tool URL:** `https://hail.so`

**Short Description (primary — paste as-is):**
Phone, SMS & email — for agents. Hail is a self-hostable, open-source (AGPLv3) communication platform for AI agents: place and receive calls, send and receive SMS and email, with delivery analytics and deliverability tracking built in. Driven via CLI, a Python SDK, OpenAPI, or a remote MCP server (Streamable HTTP — no stdio, nothing to install locally).

**Short Description (expanded — use only if the field visibly allows more length):**
Hail gives an AI agent a real phone number, inbox, and messaging line. It places and receives voice calls, sends and receives SMS, and sends and receives email — with per-channel delivery analytics and deliverability tracking (bounces, complaints, opens, clicks) built in, not bolted on. No dashboard required: an agent drives it directly through the `hail` CLI, the `hail-sdk` Python package, a documented OpenAPI spec, or a remote MCP server over Streamable HTTP. Self-host the whole stack with `docker compose up` on your own Twilio and AWS SES accounts, or run it managed at hail.so. Full source, no asterisks: it's AGPLv3.

**Category (dropdown selection):** Productivity (fallback: Other — the picker has no "Developer Tools"/"Communication" option as of this draft)

**Pricing (dropdown selection):** Freemium — self-hosted is free (AGPLv3); Hail Cloud at hail.so is usage-based (per-minute voice, per-email). Free / Paid / Open Source are the other three options on the form if Freemium is ever removed; Open Source is the next-best single-word fallback.

**Your Name:** \<submitter's real name\>

**Your Email:** `redouane.a.achouri@gmail.com`

**Repo:** `https://github.com/hail-hq/hail`

**License:** AGPL-3.0-or-later — [`LICENSE`](../../LICENSE)

**Logo asset (only if an upload field appears — none was confirmed on the public page):** `hail-website/public/assets/monogram-512.png` (square mark; `hail-website/public/assets/avatar-1024.png` as an alternate raster)

## Notes

- Free submission, no account/login required, no fee observed anywhere on the page.
- Single-person editorial review: the page states "Matt will review it." No published turnaround time — this is a manual queue, not an automated listing.
- A CAPTCHA gates the form, so the actual submit click must be done by a human.
- Confirmed form fields (public page, no login wall): Tool Name, Tool URL, Short Description, Category (dropdown), Pricing (dropdown), Your Name, Your Email, plus an optional newsletter opt-in. No logo/screenshot upload and no tags field were found on the public page — if either appears once the form is opened, use the logo asset noted in Content and skip tags (none recorded).
- Category dropdown as last checked: Chat, Generative Art, Generative Video, Generative Code, Text-To-Speech, Search Engines, Productivity, Marketing, Design, Writing, Music, Research, Education, Finance, Healthcare, Legal, Sales, Customer Support, Other. Nothing communications/dev-tools-specific exists; Productivity is the closest fit.
- Pricing dropdown as last checked: Free, Freemium, Paid, Open Source.
- Voice is Twilio-backed, email (send + receive) is AWS SES-backed — `core/hailhq/core/providers/voice/twilio.py` and `core/hailhq/core/providers/email/ses.py`. No other carrier/vendor is wired up; don't name any other provider in the listing copy.
- **SMS is stated as a shipped, present-tense product capability** in the Content copy above, per this repo's feature-claim policy (core-capability claims — voice, SMS, email, analytics, deliverability — are written as shipped regardless of implementation-milestone state). However, as of this draft there is **no SMS provider adapter** in `core/hailhq/core/providers/` and **no `send_sms`/`list_sms` tool** in the MCP surface (`mcp/hailhq/mcp/tools.py` exposes only call and email tools plus event/stats readers) — SMS is spec'd and approved (`docs/superpowers/specs/2026-07-06-sms-support-design.md`) but not implemented. Do not name a carrier for SMS, and do not claim an `hail sms` CLI command or an MCP SMS tool exists if a reviewer asks for a demo.
- **Corrected 2026-07-07: `https://mcp.hail.so` (Hail Cloud) IS live today**, not "coming soon" as the root `README.md`'s quickstart comment states — that comment is stale. Verified directly: `curl -i https://mcp.hail.so/` returns `401` with `WWW-Authenticate: Bearer ... resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"`, matching `docs/setup/mcp.md` and the real `oauth-rs` mode in `mcp/hailhq/mcp/auth.py`. Self-host (`hail mcp endpoint`) is also real and valid — both are live options.
- Once live, capture the final listing URL here.
