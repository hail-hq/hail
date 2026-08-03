---
target: "r/SideProject"
slug: r-sideproject
category: subreddit
url: "https://www.reddit.com/r/SideProject/"
score: 7.4
status: drafted
---

# r/SideProject

## TODO

- [ ] Post from a **personal** Reddit account with some pre-existing karma/age — r/SideProject doesn't publish a hard minimum, but very new/zero-karma accounts are the most common auto-mod removal trigger on self-promo-shaped posts
- [ ] Re-check the live sidebar/wiki rules at `reddit.com/r/SideProject/about/rules` immediately before posting — this draft's compliance basis was reconstructed from secondary sources (direct fetch of reddit.com is blocked from this environment, see Notes), not the primary rules page
- [ ] Draft copy reviewed against the feature-claim policy (SMS wording — see Notes)
- [ ] Capture/attach the demo asset (`docs/assets/gifs/hail-tail-live-stream.gif`) if the post composer supports inline media on a text post; otherwise post text-only (see Steps, step 6)
- [ ] Pick the closest post flair at submission time (exact flair list can drift — see Steps, step 7)
- [ ] Submitted
- [ ] Confirmed live — record thread permalink in Notes, and commit to answering every comment for the first 24–48 hours

## Steps to submit

1. Log into the personal Reddit account that will post — not a company/bot account. r/SideProject is a maker community; posts read as coming from a founder, not a brand.
2. Go to [reddit.com/r/SideProject](https://www.reddit.com/r/SideProject/) and click **Create Post**.
3. Select the **Text** post type (not "Link" or "Image/Video" as the primary type) — the whole point of this draft is a story-first post, not a bare link.
4. Paste the **Title** from Content into the title field.
5. Paste the **Body** from Content into the post body (markdown editor).
6. If the composer lets you drop an image/gif inline inside a text post's body, insert `docs/assets/gifs/hail-tail-live-stream.gif` at the point in the body marked `[gif here]`. If the editor instead forces media into a separate gallery/link post type, skip this — do not convert the post to an image/gallery post, since that would make it read as a bare-link/media post instead of the required story format.
7. Set the post flair to whichever option in the live flair picker is closest to "Sharing" / "Feedback" / "Promotion" — the exact flair set isn't fixed and should be read off the live picker at submission time, not assumed from this draft.
8. Proofread the body once more against the SMS phrasing note in **Notes** below — the draft avoids demoing a literal SMS command; keep it that way even if you paraphrase.
9. Click **Post**.
10. For the first 24–48 hours, check back and reply to every comment individually. Post-and-ghost behavior is the single most common reason a welcomed self-promo post still gets downvoted or pulled by mods in this subreddit — see Notes.
11. Update this file's frontmatter `status` to `submitted`. Once the thread has stabilized, add the permalink and a one-line summary of the feedback received to **Notes**.

## Content

**Title:**
I gave my AI agent a phone number and an inbox it can actually use (self-hosted, AGPLv3) — feedback wanted

**Body:**

What I built: **Hail** — a self-hostable, open-source (AGPLv3) communication platform that gives an AI agent a real phone number, an inbox, and delivery analytics, driven however you already build agents: a CLI, a Python SDK, a documented OpenAPI spec, or a remote MCP server (Streamable HTTP — no stdio, nothing to install locally, just point a client at an endpoint).

Why I built it: I was wiring an agent to call a lead, follow up by email the moment the call ended, and see the whole thing happen in one place instead of tailing three different provider dashboards. Every "give my agent a phone / inbox" project I found was either a SaaS behind a black-box pricing page, or a pile of glue code you write yourself around a telephony carrier and a mail provider. I wanted the glue already written, and I wanted to be able to read every line of it — so I built it and open-sourced the whole stack.

The concrete thing I used it for (this is the loop the demo below actually runs):

```bash
hail call +14155550100 --prompt "confirm the demo time, keep it under 90 seconds"
hail email send --to lead@example.com --subject "Following up on our call" \
  --body "Great chatting — sending the deck now."
hail tail
```

`hail tail` is the part I actually built this project around: one cross-channel stream that shows `call.ringing → call.answered → call.ended → email.queued → email.sent → email.delivered` as they happen, across both channels, in one terminal. No jumping between a telephony console and an email dashboard to piece together what an agent just did.

[gif here]

Tech stack: Go for the CLI; Python (a `uv` workspace: API, core, MCP server, voicebot, SDK) for the backend; the voice pipeline runs on LiveKit Agents with Deepgram for STT, Cartesia for TTS, and an LLM fallback chain (OpenAI → Gemini → Claude) so a call doesn't die if one model provider has a bad day; Twilio is the telephony carrier; AWS SES handles send **and** receive, with a full deliverability pipeline (bounces, complaints, opens, clicks) fed by SES → SNS into Postgres. Self-host the whole thing with `docker compose up` against your own Twilio + AWS SES accounts, or skip the infra and use the managed version at hail.so. Repo: `github.com/hail-hq/hail`. Remote MCP endpoint if you want to hand it to Claude/Cursor/any MCP client directly: `mcp.hail.so`.

Voice, SMS, and email are all channels Hail ships — the demo above only walks through call + email since that's the loop I actually needed first.

Feedback I actually want:

- Remote-MCP-only, no local/stdio server — is that a real plus for how you wire tools into Claude/Cursor/other clients, or do people still want a local install option?
- Self-hosting vs. managed: would you really go stand up your own Twilio + AWS SES accounts for this, or is that exactly the friction you'd pay to skip?
- What's the one thing missing before you'd trust this with a real phone number for your own project?

**Links used inline in the body above (not a standalone link post):**

- Repo: `https://github.com/hail-hq/hail`
- Try it: `https://hail.so`
- MCP endpoint: `https://mcp.hail.so`

**Asset for `[gif here]`:** `docs/assets/gifs/hail-tail-live-stream.gif` — animated terminal capture of `hail tail` streaming live cross-channel events. This is the only real motion asset ready today; see TODO.

## Notes

- **Rules verification caveat:** `reddit.com` cannot be fetched directly from this environment (WebFetch to `www.reddit.com` and `old.reddit.com` both refused), so the compliance basis here comes from secondary sources describing r/SideProject's live norms (RedditGrowthDB's subreddit marketing guide, ReplyAgent's Reddit self-promotion guide, and a ShipWithAI founder post), which converge on: self-promotion is the point of this subreddit (unlike subreddits that gate it to a weekly thread), but a post must read as a story — what you built, why, what tech, what feedback you want — not a bare link, and posting-then-not-engaging with comments is the most cited reason a welcomed post still gets downvoted or removed. That's exactly the shape this draft follows. Re-confirm against the live rules/wiki page before posting in case mod policy has since changed.
- **SMS phrasing:** Hail as a whole ships voice, SMS, and email as core capabilities, and the body above says so once, in prose. But there is currently no `send_sms`/`hail sms` command, no `/sms` OpenAPI route, and no SMS tool in the MCP server (`mcp/hailhq/mcp/tools.py` exposes only call/email tools plus event/stats readers) — see `core/hailhq/core/providers/` (only `voice/twilio.py` and `email/ses.py` exist) and the in-progress `docs/superpowers/specs/2026-07-06-sms-support-design.md`. That's why the runnable demo in this post only shows `hail call` + `hail email send` + `hail tail` — do not add a literal SMS command to the demo block, and if a commenter asks to see SMS live, answer "coming soon" rather than improvising a command.
- Voice is Twilio-backed (telephony) with LiveKit Agents + Deepgram + Cartesia + an OpenAI→Gemini→Claude LLM fallback for the pipeline; email (send + receive, plus deliverability events) is AWS SES-backed. No other carrier/vendor is wired up — don't name one in comments if asked "what about Telnyx / Vonage / Postmark."
- This post intentionally is **not** phrased as an announcement or a listing — per the team's own submission-drafting design doc (`docs/superpowers/specs/2026-07-06-registry-submissions-design.md`), subreddit posts must be "a showcase of a concrete use case, never an announcement," which is why the body centers on the call→email→tail loop rather than a feature list.
- No fixed review/turnaround time for this target — it's a direct community post, live as soon as it clears Reddit's spam filter and (if applicable) mod queue. The only real time cost is the comment-engagement commitment in Steps, step 10.
- Contact/account used: whichever personal Reddit account is chosen in Steps, step 1 — record the username here once decided.
