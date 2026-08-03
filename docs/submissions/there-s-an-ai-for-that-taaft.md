---
target: "There's An AI For That (TAAFT)"
slug: there-s-an-ai-for-that-taaft
category: ai-directory
url: "https://theresanaiforthat.com/launch/"
score: 4.8
status: drafted
---

# There's An AI For That (TAAFT)

## TODO

- [ ] Decide submission track: paid self-serve form ($49–$347, guaranteed listing + review) vs. free monthly lottery-odds thread — record the choice and price paid (if any) in Notes
- [ ] Create/confirm a TAAFT account to submit under (the launch form requires a logged-in account)
- [ ] Reconfirm the live pricing tiers at submission time (recorded as $49–$347 — TAAFT changes tier names/prices periodically)
- [ ] Draft copy reviewed against the feature-claim policy (SMS capability claim vs. no SMS provider/tool wired up yet — see Notes)
- [ ] Logo/screenshot assets attached from `hail-website/public/assets/`
- [ ] Tool URL confirmed as `https://hail.so`
- [ ] Submitted (paid tier or lottery thread)
- [ ] Manual staff review passed — confirmed live, listing URL recorded in Notes

## Steps to submit

1. Go to [theresanaiforthat.com/launch](https://theresanaiforthat.com/launch/).
2. Log in or create a TAAFT account if prompted — the launch form is gated behind an account.
3. Choose a submission track on the launch page: a paid self-serve tier (priced $49–$347 depending on placement/speed at time of submission) for a guaranteed, faster-reviewed listing, or the free option, which enters the tool into a monthly community thread with lottery-style odds of being picked and reviewed. Pick paid unless the submitter explicitly wants to gamble on the free thread.
4. In the **Tool URL** / website field, enter: `https://hail.so`
5. In the **Tool Name** field, enter: `Hail`
6. Paste the one-liner from **Content** below into the tagline/short-description field.
7. Paste the longer description from **Content** into the full description field.
8. Enter the tags/keywords listed in **Content** into the tags field (comma-separated or one-per-box, depending on the live form's input style).
9. Upload the logo asset from **Content** (square PNG) when the asset-upload step appears.
10. If a pricing-model field appears, select **Freemium** / **Open Source** (self-hosted is free under AGPLv3; Hail Cloud at hail.so is usage-based) — pick whichever single option the live form offers.
11. Complete checkout if the paid tier was selected; otherwise confirm placement in the free monthly thread.
12. Submit. TAAFT staff manually review every submission before it goes live — there is no instant auto-publish.
13. Once approved and live, update this file's frontmatter `status` to `submitted`, then record the final listing URL in **Notes**.

## Content

**Tool Name:** Hail

**Tool URL:** `https://hail.so`

**One-liner (tagline):**
Phone, SMS & email — for agents.

**Short description:**
Hail is a self-hostable, open-source (AGPLv3) communication platform for AI agents: place and receive phone calls, send and receive SMS, and send and receive email — with delivery analytics and deliverability tracking built in.

**Full description:**
Hail gives an AI agent a real phone number, inbox, and messaging line. It places and receives voice calls, sends and receives SMS, and sends and receives email — with per-channel delivery analytics and deliverability tracking (bounces, complaints, opens, clicks) built in, not bolted on. No dashboard required: an agent drives it directly through the `hail` CLI, the `hail-sdk` Python package (`pip install hail-sdk`, imports as `hail`), a documented OpenAPI spec, or a remote MCP server over Streamable HTTP — no stdio, nothing to install locally. Self-host the whole stack with `docker compose up` against your own Twilio and AWS SES accounts, or run it managed at hail.so. Full source, no asterisks: AGPLv3.

**Tags/keywords:** AI agents, voice calls, SMS, email, communication API, MCP server, developer tools, self-hosted, open source, CLI, Python SDK, deliverability, automation

**Category:** Developer Tools / Productivity (closest single-category fit — TAAFT has no dedicated "agent communication" category)

**Pricing model:** Freemium / Open Source — self-hosted is free (AGPLv3); Hail Cloud at hail.so is usage-based (per-minute voice, per-email).

**Repo:** `https://github.com/hail-hq/hail`

**License:** AGPL-3.0-or-later — [`LICENSE`](../../LICENSE)

**Logo asset:** `hail-website/public/assets/monogram-512.png` (square mark, upload-ready). Alternate raster: `hail-website/public/assets/avatar-1024.png`. Social/OG card if a screenshot slot exists: `hail-website/public/assets/og-card-1200x630.png`.

## Notes

- Two submission tracks exist: a **paid self-serve form** ($49–$347, price scales with placement/speed) that guarantees a listing pending review, and a **free monthly lottery-odds thread** where inclusion is not guaranteed and odds are effectively random. Recommend paid unless the submitter is fine with the free tier's uncertainty; record whichever is chosen and the amount paid here.
- **Manual staff review applies regardless of track** — no tier bypasses editorial review, it only affects queue priority/speed.
- Voice is Twilio-backed, email (send + receive) is AWS SES-backed — `core/hailhq/core/providers/voice/twilio.py` and `core/hailhq/core/providers/email/ses.py`. No other carrier/vendor is wired up; don't name any other provider in the listing copy.
- **SMS is stated as a shipped, present-tense product capability** in the Content copy above, per this repo's feature-claim policy (core-capability claims — voice, SMS, email, analytics, deliverability — are written as shipped regardless of implementation-milestone state). However, as of this draft there is **no SMS provider adapter** in `core/hailhq/core/providers/` and **no `send_sms`/`list_sms` tool** in the MCP surface (`mcp/hailhq/mcp/tools.py` exposes only call and email tools plus event/stats readers) — SMS is spec'd and approved (`docs/superpowers/specs/2026-07-06-sms-support-design.md`) but not implemented. Do not name a carrier for SMS, and do not claim an `hail sms` CLI command or an MCP SMS tool exists if a reviewer asks for a demo.
- **Corrected 2026-07-07: `https://mcp.hail.so` (Hail Cloud) IS live today**, not "coming soon" as the root `README.md`'s quickstart comment states — that comment is stale. Verified directly: `curl -i https://mcp.hail.so/` returns `401` with `WWW-Authenticate: Bearer ... resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"`, matching `docs/setup/mcp.md` and the real `oauth-rs` mode in `mcp/hailhq/mcp/auth.py`. Self-host (`hail mcp endpoint`) is also real and valid — both are live options.
- Once live, capture the final listing URL and which track/price was used here.
