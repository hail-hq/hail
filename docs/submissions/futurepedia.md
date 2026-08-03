---
target: "Futurepedia"
slug: futurepedia
category: ai-directory
url: "https://www.futurepedia.io/submit-tool"
score: 3.5
status: drafted
---

# Futurepedia

## TODO

- [ ] Decide whether the $247/$497 spend is worth it before doing anything else — this is a paid-only submission, no free tier (see Notes)
- [ ] Confirm which tier to buy: Basic ($247, 7-day publish, currently sold out per last check) or Verified ($497, 2-business-day publish, verified checkmark)
- [ ] Get sign-off from whoever owns marketing spend to actually run the checkout
- [ ] Logo asset ready: `hail-website/public/assets/monogram-512.png` (square) or `hail-website/public/assets/avatar-1024.png`
- [ ] Screenshot/demo asset prepared if the paid tier's form asks for one (not confirmed — form fields beyond pricing weren't fully enumerated; verify at checkout)
- [ ] Draft copy reviewed against the feature-claim policy (see Notes on SMS/vendor accuracy)
- [ ] Account created on futurepedia.io if checkout requires one
- [ ] Payment completed and listing submitted
- [ ] Confirmed live — record listing URL and which tier was purchased in Notes

## Steps to submit

1. Go to [futurepedia.io/submit-tool](https://www.futurepedia.io/submit-tool).
2. Pick a tier: **Basic Listing** ($247, published within 7 days) or **Verified Listing** ($497, published within 2 business days, gets a verified checkmark and an enhanced listing page). Enterprise is custom-priced and out of scope here.
3. Enter the **Tool URL**: `https://hail.so`
4. Fill in the **Name** field with: `Hail`.
5. Paste the **one-liner** from **Content** below into the tagline/short-description field.
6. Paste the **long description** from **Content** below into the main description field.
7. Select a **category**: closest match is "Productivity" or "AI Agents" / "Developer Tools" — pick whichever the category picker actually offers; there is no "communications" category confirmed to exist.
8. Add **tags/keywords** from **Content** below if a tags field is offered.
9. Upload the **logo** from `hail-website/public/assets/monogram-512.png`.
10. If a video-upload field appears (both paid tiers mention "add a video to your page"), skip it — no demo video exists yet. Leave it blank rather than link a placeholder.
11. Check the agreement checkbox for the Futurepedia Terms of Service, HubSpot Website Terms of Use, and Privacy Policy — read them once before checking, don't blind-accept.
12. Proceed to checkout and pay for the chosen tier. This is a hard paywall — there is no way to submit without payment.
13. After payment, note the confirmation/order details in **Notes**.
14. Wait for editorial approval and publication (7 days for Basic, 2 business days for Verified). Both tiers are explicitly "subject to editorial approval," so publication isn't guaranteed by payment alone.
15. Once live, update this file's frontmatter `status` to `submitted` (then `live`/confirmed once it appears), and record the listing URL in **Notes**.

## Content

**Name:** Hail

**Tool URL:** `https://hail.so`

**One-liner:** Phone, SMS & email — for agents.

**Description (long):**
Hail is a self-hostable, AGPLv3 communication platform built for AI agents. It gives an agent a real phone number, inbox, and messaging line — place and receive calls, send and receive SMS, send and receive email, and read back structured events and analytics — all through one remote MCP server (Streamable HTTP, no local install or stdio package). Connect a client to `https://mcp.hail.so`, authorize once via OAuth, and the agent gets tools immediately. Self-hosters run the same server with a static API key instead of OAuth. Also available via CLI (`hail`), a Python SDK, and OpenAPI for non-MCP integrations. Deliverability tooling and per-channel analytics are built in, not bolted on.

**Category:** AI Agents / Developer Tools / Productivity (pick closest available on the form — see Steps)

**Tags/keywords (if offered):** communication, voice, phone calls, sms, email, ai agents, mcp, self-hosted, open-source, developer tools

**License:** AGPL-3.0-or-later — [`LICENSE`](../../LICENSE)

**Logo asset:** `hail-website/public/assets/monogram-512.png` (square mark; `hail-website/public/assets/avatar-1024.png` as an alternate raster)

**Contact email (if requested):** `hi@hail.so`

**GitHub:** `https://github.com/hail-hq/hail`

## Notes

- **Paid-only, no confirmed free tier.** Basic is $247 (was showing "sold out" at last check — reconfirm availability before starting checkout), Verified is $497. Both require payment before submission; there is no draft-then-pay-later flow confirmed. Don't start checkout without a go-ahead on spend.
- **Review turnaround:** 7 days (Basic) or 2 business days (Verified), and both are explicitly subject to editorial approval — payment doesn't guarantee acceptance or a specific publish date.
- **Agreement required:** checkbox covers the Futurepedia Terms of Service, the HubSpot Website Terms of Use, and the Privacy Policy (Futurepedia's submission flow runs on HubSpot).
- SMS is a shipped, present-tense capability of Hail as a whole (voice, SMS, email) per product copy, but there is no `send_sms`/`list_sms` tool wired into the MCP tool surface (`mcp/hailhq/mcp/tools.py` exposes only call and email tools plus event/stats readers), and no SMS provider adapter exists yet in `core/hailhq/core/providers/` (only `voice/twilio.py` and `email/ses.py` — see also the in-progress design doc `docs/superpowers/specs/2026-07-06-sms-support-design.md`). Keep SMS out of any field that implies it's an MCP _tool_ or that names a specific SMS carrier; SMS-over-MCP and the SMS carrier integration are "coming soon."
- Voice is Twilio-backed, email is SES-backed (`core/hailhq/core/providers/voice/twilio.py`, `core/hailhq/core/providers/email/ses.py`) — no other carrier/vendor is wired up yet, so don't name any other provider in the listing copy.
- Form fields beyond pricing/ToS weren't fully enumerated from the public page (name/description/category/tags/logo/video are inferred from typical Futurepedia listing pages and the "add a video" mention in pricing copy) — confirm the actual field set once past the paywall/checkout, and adjust which **Content** items get pasted where.
- No demo video exists for Hail yet; don't fabricate one or link a placeholder just because the form offers a video slot.
