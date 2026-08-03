---
target: "Hacker News – Show HN"
slug: hacker-news-show-hn
category: dev-directory
url: "https://news.ycombinator.com/showhn.html"
score: 7
status: drafted
---

# Hacker News – Show HN

## TODO

- [ ] HN account exists and is old enough / karma'd enough to post (new accounts can be rate-limited or shadow-caught by spam filters — use an established account if available)
- [ ] Confirm `docker compose up` on a clean clone still boots green (this is the demo the whole thread will hammer on)
- [ ] Confirm `hail call`, `hail email send`, and `hail mcp endpoint` all work against a fresh local stack with no Hail Cloud account
- [ ] Pick and pin the exact repo commit/tag being shown, so instructions in the post don't drift
- [ ] Write and proofread the first-comment explainer (below) — post it within 60 seconds of the submission going live
- [ ] Block out 4-6 hours after posting to answer every comment (submission time should be a weekday, ~8-10am US Eastern for best front-page odds)
- [ ] Title character-count check (HN truncates around 80 chars)
- [ ] Draft reviewed
- [ ] Submitted
- [ ] Confirmed live and first comment attached

## Steps to submit

1. Log in to Hacker News at https://news.ycombinator.com with the account chosen above.
2. Go to https://news.ycombinator.com/submit.
3. In **Title**, paste the title from Content below. Do not add "Show HN:" yourself if the field auto-detects — HN's submit form only prepends it when you post from `/showhn`-style flow; if using the plain submit form, type the title exactly as `Show HN: Hail – Phone, SMS & email for AI agents (CLI, SDK, MCP, self-hostable)`.
4. In **URL**, paste `https://github.com/hail-hq/hail` (the repo — Show HN posts should link to something people can immediately inspect/clone, not a marketing page).
5. Leave **Text** blank (Show HN posts should be link posts; the explainer goes in the first comment, not the submission body).
6. Click **submit**.
7. Immediately open the new post's own comment thread (URL will look like `https://news.ycombinator.com/item?id=NNNNNNNN`).
8. Paste the first-comment explainer from Content into the top-level comment box and submit it — this is the standard Show HN pattern (context/backstory goes in the author's first comment, not the title/URL).
9. Watch the thread for the next several hours. Reply to every substantive comment — HN penalizes absentee submitters, and the first 20-30 minutes decide whether it holds a front-page slot.
10. If someone reports a broken step in the quickstart, fix it in the repo and reply with a correction in-thread; do not edit the original post (HN posts are effectively immutable in practice for anything but title/URL early on).

## Content

**Title** (paste into Submit form's Title field):

```
Show HN: Hail – Phone, SMS & email for AI agents (CLI, SDK, MCP, self-hostable)
```

**URL**:

```
https://github.com/hail-hq/hail
```

**First-comment explainer** (post immediately after submission goes live):

```
Hi HN, maker here.

Hail is a self-hostable communication platform for AI agents: voice calls and
email today, consumed however your agent stack already talks to tools — CLI,
Python SDK, OpenAPI, or a remote MCP server (Streamable HTTP, no stdio/local
install required).

Why: every "AI agent calls a phone number" or "AI agent sends an email" demo
I'd seen was a pile of Twilio/SES glue duct-taped directly into someone's
agent code, with no clean API boundary and no way to run it yourself. Hail
is that glue, packaged as one thing: it does the carrier handling and the
voice pipeline (STT/TTS/turn-detection), and your agent just calls an
endpoint or an MCP tool.

Try it with zero signup, in one shell:

  git clone https://github.com/hail-hq/hail
  cd hail
  cp .env.example .env      # fill in Twilio + LiveKit Cloud + Deepgram/Cartesia
                             # + one LLM key (OpenAI/Gemini/Anthropic)
  docker compose up

Then:

  hail call +1XXXXXXXXXX --prompt "be brief" --recipient-consent
  hail email send --to you@example.com --subject hi --body "hello from hail" --recipient-consent
  hail mcp endpoint          # Streamable HTTP URL — point Claude/Cursor/etc. at it

(`--recipient-consent` is a required attestation flag — Hail rejects
send/call requests without it; it doesn't verify consent for you, you're
on the hook for a lawful basis under TCPA/ePrivacy/PECR/CAN-SPAM/GDPR.)

No hosted account needed to try any of this — the whole stack runs on your
own machine with your own provider keys. (There's also a hosted version at
hail.so if you don't want to run infra, but that's optional, not required
for the demo.)

Code's AGPLv3. Stack is Python (FastAPI) + LiveKit for the voice transport,
Deepgram for STT, Cartesia/ElevenLabs for TTS, Twilio for telephony, SES for
email. Docs: docs/setup/twilio.md, docs/setup/livekit-cloud.md,
docs/setup/aws-ses.md, docs/setup/mcp.md in the repo.

Known gaps, in progress: inbound calls, SMS send/receive, and a Telnyx
backend are on the roadmap but not wired up yet (see the Milestones table
in the README) — voice and email (send + receive, including custom sender
domains) are what's shipped and load-bearing today.

Happy to answer anything — architecture, why LiveKit over raw SIP, why MCP
over a plugin model, pricing, whatever.
```

**Assets**: no OG-card image, wordmark file, or voice-demo audio clip currently exist in the repo (`web/` — the `@hail-hq/web` app — has no `public/` directory at all yet, and there is no `hail-website/` directory anywhere in the repo). If a link preview or social crosspost needs visual assets, produce and add them before this goes out — don't reference paths that don't exist.

## Notes

- **SMS is not yet shipped** — verified against `core/hailhq/core/providers/` (only `voice/` and `email/` subpackages exist, no `sms/`) and the API/CLI/MCP surfaces: no `/sms` route, no `hail sms` command, no SMS MCP tool, and the README's own Milestones table lists SMS outbound/inbound as unchecked. The title above says "Phone, SMS & email" per brand copy conventions elsewhere, but the first-comment explainer deliberately does NOT claim SMS as shipped and calls it out as a roadmap gap — HN is a hands-on audience with no gated signup in front of them, so an unshippable claim gets caught within minutes and burns credibility for the whole thread. If SMS actually ships before this goes out, delete this note and add it to the try-it commands.
- **Demo commands need `--recipient-consent`**: `CallCreate`/`EmailCreate` (`core/hailhq/core/schemas.py`) make `recipient_consent` a required field with no default — the API 422s without it — and the CLI flag (`cli/internal/cmd/call.go`, `cli/internal/cmd/email.go`) defaults to `false`. The explainer's example commands now include the flag so a reader who pastes-and-runs them doesn't hit a 422 on the very first command.
- Show HN convention: title + link only in the submission; the "why we built this" narrative belongs in the maker's first comment. Do not put the explainer text in the HN submission's Text field — that's for Ask HN / text posts, not Show HN link posts.
- Best submission window: weekday, US morning (front page turnover is fast; avoid Fri evening/weekend for reach, though Show HN skews slightly more forgiving on timing than regular submissions).
- No formal review/moderation turnaround — HN posts go live immediately; the risk is the second-chance pool / flags, not an approval queue. Community self-moderates via voting and (occasionally) mod intervention (dang) for title/URL edits if something's clearly wrong.
- Contact for in-thread replies: whoever owns the HN account posting this should self-identify as "maker" in the first comment (done above) — don't post anonymously or via a company account with no history, it reads as marketing and gets flagged faster.
- Do not cross-post the identical text to Show HN and Product Hunt same day — HN regulars notice and it reads as a spray-and-pray launch, which invites "low effort" flags.
