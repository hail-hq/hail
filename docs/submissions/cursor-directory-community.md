---
target: "Cursor directory (community)"
slug: cursor-directory-community
category: mcp-registry
url: "https://cursor.directory/plugins/new"
score: 4
status: drafted
---

# Cursor directory (community)

## TODO

- [ ] Confirm `https://mcp.hail.so` is publicly reachable (it is — Hail Cloud's standing MCP endpoint; expect a `401` + `WWW-Authenticate` challenge, not a `200`, since the server is OAuth-protected)
- [ ] Draft copy reviewed against the feature-claim policy
- [ ] Logo asset ready (`hail-monogram.svg` or `avatar-1024.png`), in case the form accepts an image upload/URL
- [ ] No account/login requirement confirmed at submission time (unofficial community form — mechanics unverified, see Notes)
- [ ] Submitted via cursor.directory/plugins/new
- [ ] Confirmed live — record listing URL in Notes

## Steps to submit

1. Go to [cursor.directory/plugins/new](https://cursor.directory/plugins/new).
2. Fill in **Name** with: `Hail`.
3. Fill in **Description** with the one-liner from **Content** below (use the longer description instead if the field allows more length).
4. Fill in the **config/URL** field with the MCP server URL: `https://mcp.hail.so` (paste the JSON config block from **Content** below if the field expects a client config snippet rather than a bare URL).
5. If a **category/tag** field is offered, pick the closest match to "MCP server" / "communication" (this listing is unofficial and community-curated — there is no formal review, so pick the best available option rather than waiting on guidance).
6. If a **repo/link** field is offered, paste `https://github.com/hail-hq/hail`.
7. If a logo/image upload is offered, attach the asset from **Content** below.
8. Submit the form. This is an unofficial community form with no stated vetting process — expect the listing to either appear immediately or not require further action.
9. Once the listing appears (or after a reasonable wait with no confirmation email/page), update this file's frontmatter `status` to `submitted` and record the listing URL (or lack of one) in **Notes**.

## Content

**Name:** Hail

**One-liner:**

<!-- copy:tagline -->

Give your AI a phone number and an email inbox.

<!-- /copy -->

**Description (long):**

<!-- copy:long -->

Hail gives your AI agent a real phone number and an email inbox. Your agent can do any work people do on the phone and in email.

Use it as an AI phone agent or an AI answering service. It handles calls, texts (SMS), and email day and night, and passes urgent calls to a person.

Hail keeps the whole conversation. When someone calls after an email, your agent already knows what was said.

Get a local or toll-free number in 120+ countries. Hail helps with the paperwork some countries need, such as US A2P 10DLC registration for texts.

Connect Hail to Claude, ChatGPT, Cursor, or another MCP client in minutes, with nothing to install. Or build it into your app with the API, CLI, or Python SDK.

Hail is open source. Use Hail Cloud, or host it yourself.

<!-- /copy -->

**Config/URL field:**

```json
{
  "mcpServers": {
    "hail": {
      "url": "https://mcp.hail.so"
    }
  }
}
```

(Server URL alone, if the field wants a bare string: `https://mcp.hail.so`)

**Repo link:** `https://github.com/hail-hq/hail`

**Tags/keywords (if offered):** communication, voice, phone, sms, email, agents, self-hosted, open-source

**License:** AGPL-3.0-or-later — [`LICENSE`](../../LICENSE)

**Logo asset:** `hail-website/public/assets/brand/logo/hail-monogram.svg` (square mark; `hail-website/public/assets/brand/logo/avatar-1024.png` as raster fallback)

## Notes

- 2026-11-16: could not check whether Hail is listed. cursor.directory returned HTTP 429 (rate limit) to every scripted request. Check <https://cursor.directory/plugins/new> in a browser.

- Listed as **"Unofficial community form"** with **no formal vetting** per the target brief — treat this as low-effort/low-certainty: no confirmation email, moderation queue, or SLA should be assumed. If the form submits with no visible acknowledgment, that's expected, not a failure.
- 2026-11-16: SMS is shipped. The MCP server exposes `send_sms`, `get_sms` and `list_sms` (`mcp/hailhq/mcp/tools.py`). Earlier notes that said SMS was not shipped were removed.
- Voice is Twilio-backed, email is SES-backed (`core/hailhq/core/providers/voice/twilio.py`, `core/hailhq/core/providers/email/ses.py`) — no other carrier/vendor is wired up yet, so don't name any other provider in the listing copy.
- Couldn't verify the live form's exact field set (name/description/config vs. a longer multi-field layout) — cursor.directory's `/plugins/new` page wasn't fetched for this draft; confirm actual fields at submission time and adjust which **Content** items get pasted where.
- No documented review turnaround, contact channel, or edit/removal process found for this target — treat as fire-and-forget until proven otherwise.
