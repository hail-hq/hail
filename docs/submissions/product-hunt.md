---
target: "Product Hunt"
slug: product-hunt
category: dev-directory
url: "https://www.producthunt.com/launch"
score: 7.2
status: drafted
---

# Product Hunt

> 🛑 **HOLD — do not publish yet.** Explicit user instruction (2026-07-07): this draft is ready, but the actual launch is intentionally on hold pending a decision on launch-day coordination. Do not schedule or submit until that hold is lifted.

## TODO

- [ ] Confirm/create a **personal** Product Hunt maker account for the founder (producthunt.com profile, not a company/team page) — the launch must post as an individual maker
- [ ] Fill out maker profile completely (photo, bio, X/GitHub links) — thin profiles get less trust on launch day
- [ ] Gallery assets finalized — only 1 of the recommended 3+ is confirmed in this repo today (`docs/assets/gifs/hail-tail-live-stream.gif`); the static brand assets in Content (og card, wordmark, monogram) live in the separate `hail-website` repo, not this `hail` monorepo checkout — confirm they actually exist there before relying on the paths below. Also capture 2 more real product screenshots (e.g. `hail call` + `hail email send` CLI output, the MCP client picker at hail.so/mcp) before scheduling
- [ ] Product icon uploaded (square, separate from gallery — see Content)
- [ ] Tagline, description, topics drafted (below) reviewed against the feature-claim policy
- [ ] Maker's first comment drafted (below) — must be posted the moment the launch goes live
- [ ] Pick a launch date/time: weekday, 12:01 AM PT, checked against producthunt.com/leaderboard for competing high-profile launches that day
- [ ] Founder has cleared their full PT-day calendar to reply to every comment (hard requirement of "full listing" launches)
- [ ] Listing scheduled on Product Hunt
- [ ] Live — comment thread monitored all day
- [ ] Confirmed live — record final listing URL and rank in Notes

## Steps to submit

1. Go to [producthunt.com/launch](https://www.producthunt.com/launch) and log in with the founder's **personal** account (not a company page). If only a company page exists today, create/switch to a personal maker profile first — Product Hunt requires a real person as the maker of record.
2. Click **Launch your product** to start a new (draft) listing.
3. **Name:** paste `Hail` (see Content).
4. **Tagline:** paste the one-liner from Content (fits PH's tagline limit).
5. **Links:** website `https://hail.so`, and add `https://github.com/hail-hq/hail` as an additional link.
6. **Topics:** select the topics listed in Content (pick the closest live matches in PH's topic picker — exact topic names drift over time).
7. **Description:** paste the long description from Content into the pitch/description field.
8. **Gallery:** upload the assets listed in Content **in the order given** — Product Hunt uses the first gallery item as the listing thumbnail.
9. **Product icon:** upload the square icon asset from Content (separate upload slot from the gallery).
10. **Makers:** add the founder as a maker. Skip "hunter" — this is a self-hunt launch from a personal account.
11. **Pricing:** select the pricing type from Content.
12. Click **Preview** and proofread every field against Content — check for truncation on the tagline and first gallery caption.
13. Click **Schedule launch**, pick the date/time decided in TODO, and confirm.
14. At the moment the launch goes live (12:01 AM PT), post the maker's first comment from Content as the top comment on the listing — this is the pinned "why I built this" comment PH launches live or die on.
15. Throughout the full PT day, reply to every comment and upvote-comment personally — do not batch replies at the end of the day.
16. After launch, update this file's frontmatter `status` to `submitted`, and once the day closes, add the final listing URL and day-rank to **Notes**.

## Content

**Name:** Hail

**Tagline (one-liner):** Phone, SMS & email — for agents.

**Topics:** Developer Tools · Artificial Intelligence · Open Source · APIs

**Links:**

- Website: `https://hail.so`
- Repo: `https://github.com/hail-hq/hail`

**Description:**
Hail is a self-hostable, open-source (AGPLv3) communication platform for AI agents. Phone, SMS & email — for agents: your agent places and receives calls, sends and reads email, with delivery analytics and deliverability tracking built in, not bolted on. No dashboard to click through — agents drive it directly over a CLI, a Python SDK, a documented OpenAPI spec, or a remote MCP server (Streamable HTTP — no stdio, nothing to install locally). Self-host the whole stack with `docker compose up` on your own Twilio and AWS SES accounts, or run it managed at hail.so. Full source, no asterisks: it's AGPLv3.

**Install / usage snippet:**

```bash
# Self-host
git clone https://github.com/hail-hq/hail
cd hail && cp .env.example .env   # fill in Twilio, LiveKit Cloud, Deepgram, Cartesia, AWS SES, and one of OpenAI/Gemini/Anthropic
docker compose up

# CLI
hail call +14155550100 --prompt "be brief"
hail email send --to a@b.com --subject hi --body "hello"
hail tail                          # cross-channel live event stream

# Python SDK
pip install hail-sdk

# MCP (Claude, Cursor, etc.) — Streamable HTTP, no stdio
hail mcp endpoint                  # prints your self-hosted connector URL (e.g. http://<host>:8081)
# Or connect straight to Hail Cloud: https://mcp.hail.so (OAuth — click Allow when your client prompts)
```

**Pricing (PH pricing-type field):** Freemium — self-hosted is free (AGPLv3); Hail Cloud at hail.so is usage-based (per-minute voice, per-email).

**Gallery (upload in this order — first item becomes the listing thumbnail):**

1. `hail/docs/assets/gifs/hail-tail-live-stream.gif` — animated terminal demo of `hail tail` streaming live call events. Best available motion asset; use as thumbnail.
2. `hail-website/public/assets/og-card-1200x630.png` — static brand card (placeholder slide until a real product screenshot is captured — see TODO). Path is in the separate `hail-website` repo; confirm the file is actually there before uploading (not part of this `hail` monorepo checkout).
3. `hail-website/public/assets/wordmark-1200.png` — wordmark, as a clean closing slide. Same caveat: confirm in `hail-website` before uploading.

**Product icon (separate upload, square):** `hail-website/public/assets/monogram-512.png` (fallback: `hail-website/public/assets/hail-monogram.svg`) — both paths are in the `hail-website` repo; confirm presence there first.

**Maker's first comment (post immediately at launch):**

> Hey Product Hunt — I built Hail because every "give my agent a phone number / inbox" project I found was either a SaaS behind a black-box pricing page, or a pile of glue code around Twilio you had to write yourself. Hail is that glue, already written: one API — plus a remote MCP server, so a client just plugs in — for voice calls, SMS, and email. Self-host it on your own Twilio and AWS SES accounts, or run it managed at hail.so. It's AGPLv3: full source, no asterisks. I'll be here all day answering questions about the architecture, the pricing, or why voice agents are still harder than they should be.

## Notes

- Product Hunt has no formal review queue for standard launches — a scheduled listing goes live automatically at the chosen time, provided it isn't flagged as spam/duplicate. No submission-to-live turnaround to wait on; the only gate is the founder's own schedule and the comment-thread commitment on launch day.
- Contact used: redouane.a.achouri@gmail.com (founder's account email).
- Voice is Twilio-backed, email (send + receive) is AWS SES-backed — see `core/hailhq/core/providers/voice/twilio.py` and `core/hailhq/core/providers/email/ses.py`. No other carrier/vendor is wired up; don't name any other provider in the listing or comments.
- **SMS is not yet wired to a concrete endpoint** — there is no `/sms` route in `openapi/openapi.yaml`, no `hail sms` CLI command, and no SMS tool in the MCP server (`mcp/hailhq/mcp/tools.py` only exposes call/email tools). Keep SMS in the tagline/description as a stated product capability (per brand-voice policy), but do **not** demo a literal SMS command in comments or gallery captions — there isn't one to run yet. Treat it as "coming soon" if a commenter asks for a live example.
- MCP tool surface today: `place_call`, `send_email`, `get_call`, `list_calls`, `get_email`, `list_emails`, `get_email_raw`, `get_email_attachment`, `get_email_events`, `get_email_stats`, `get_events` — no SMS tools (matches the point above).
- **Corrected 2026-07-07: `https://mcp.hail.so` (Hail Cloud) IS live today**, not "coming soon" as the root `README.md`'s quickstart comment states — that comment is stale. Verified directly: `curl -i https://mcp.hail.so/` returns `401` with `WWW-Authenticate: Bearer ... resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"`, matching `docs/setup/mcp.md` and the real `oauth-rs` mode in `mcp/hailhq/mcp/auth.py`. Fine to demo `mcp.hail.so` as a working connector — self-host (`hail mcp endpoint`) is also real and valid, both are live options.
- Gallery is thin right now (1 real asset, confirmed in this repo). The other two gallery images and the product icon live in the separate `hail-website` repo, not this checkout — their presence there is unconfirmed from here. Do not schedule the launch until at least 2 additional genuine product screenshots exist and the brand-asset paths are confirmed — a thumbnail-only or broken-asset launch reads as unfinished on Product Hunt.
- PH topic names drift; reconcile the four listed topics against the live picker at submission time and swap in the closest exact matches.
