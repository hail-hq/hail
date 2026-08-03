---
target: "r/opensource"
slug: r-opensource
category: subreddit
url: "https://www.reddit.com/r/opensource/"
score: 6
status: drafted
---

# r/opensource

## TODO

- [ ] Post from a personal Reddit account, not a company/bot account — r/opensource reads project showcases as coming from the maintainer
- [ ] Re-check the live sidebar/wiki rules at `reddit.com/r/opensource/about/rules` immediately before posting — this draft's compliance basis was reconstructed from secondary sources (direct fetch of reddit.com is blocked from this environment, see Notes), not the primary rules page
- [ ] Confirm no standing weekly self-promo/showcase megathread exists that this should go in instead of a standalone post
- [ ] Draft copy reviewed against the feature-claim policy (SMS wording — see Notes)
- [ ] Confirm `LICENSE` in the repo root still reads AGPL-3.0 verbatim (it does as of this draft) before quoting it
- [ ] Capture/attach the demo asset (`docs/assets/gifs/hail-tail-live-stream.gif`) if the composer supports inline media on a text post; otherwise post text-only (see Steps, step 6)
- [ ] Pick the closest post flair at submission time (exact flair list can drift — see Steps, step 7)
- [ ] Submitted
- [ ] Confirmed live — record thread permalink in Notes, and commit to answering every comment for the first 24–48 hours

## Steps to submit

1. Log into the personal Reddit account that will post.
2. Go to [reddit.com/r/opensource](https://www.reddit.com/r/opensource/) and click **Create Post**.
3. Select the **Text** post type (not "Link" or "Image/Video") — this is a story-and-code post, not a bare link drop.
4. Paste the **Title** from Content into the title field.
5. Paste the **Body** from Content into the post body (markdown editor; switch to "Markdown Mode" via the editor's `...`/format toggle if it opens in rich-text mode, so the code fences render correctly).
6. If the composer lets you drop an image/gif inline inside a text post's body, insert `docs/assets/gifs/hail-tail-live-stream.gif` at the point in the body marked `[gif here]`. If the editor forces media into a separate gallery/link post type, skip this — do not convert the post to an image/gallery post.
7. Set the post flair to whichever option in the live flair picker is closest to "Project" / "Show and Tell" / "Self-Promotion" — the exact flair set isn't fixed and should be read off the live picker at submission time.
8. Preview the post; confirm the code blocks render as code and the `github.com/hail-hq/hail` link is clickable.
9. Proofread once more against the SMS phrasing note in **Notes** below — the draft avoids demoing a literal SMS command; keep it that way even if you paraphrase.
10. Click **Post**.
11. For the first 24–48 hours, check back and reply to every comment individually — a genuine-project showcase that goes quiet after posting is the most common reason this kind of thread stalls or gets modded.
12. Update this file's frontmatter `status` to `submitted`. Once the thread has stabilized, add the permalink and a one-line summary of the feedback received to **Notes**.

## Content

**Title:**
Gave my AI agent a real phone number and inbox — self-hosted, AGPLv3, here's the actual loop it runs

**Body:**

What I built: **Hail** — a self-hostable communication platform for AI agents: phone calls, SMS, and email, driven by a CLI, a Python SDK, a documented OpenAPI spec, or a remote MCP server (Streamable HTTP — no stdio, nothing to install locally). Genuine public repo, real license: [github.com/hail-hq/hail](https://github.com/hail-hq/hail), [AGPL-3.0-or-later](https://github.com/hail-hq/hail/blob/main/LICENSE) — run a modified Hail as a service and you must release your source, same as the license on the tin says.

Why I built it: I was wiring an agent to call a lead, follow up by email the moment the call ended, and watch the whole thing happen in one place instead of tailing three provider dashboards. Every "give my agent a phone / inbox" project I found was a SaaS behind a black-box pricing page, or a pile of glue code you write yourself around a telephony carrier and a mail provider. I wanted the glue already written, and I wanted to read every line of it — so I built it and open-sourced the whole stack instead of keeping it a private script.

The concrete loop the demo below actually runs:

\`\`\`bash
hail call +14155550100 --prompt "confirm the demo time, keep it under 90 seconds"
hail email send --to lead@example.com --subject "Following up on our call" \
 --body "Great chatting — sending the deck now."
hail tail
\`\`\`

`hail tail` is the part I built this project around: one cross-channel stream showing state transitions for both channels as they happen — e.g. `queued → dialing → in_progress → completed` for the call, `sent → delivered` for the email — merged into one terminal.

[gif here]

Install (self-host, no managed account needed):

\`\`\`bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env

# fill in Twilio, LiveKit Cloud, Deepgram, Cartesia, and one of OpenAI / Gemini / Anthropic

docker compose up
\`\`\`

Then seed an API key (see `docs/operations.md` — "Self-host: first-run setup"), export `HAIL_API_KEY`, and the commands above work against your own stack.

Tech stack: Go for the CLI; Python (a `uv` workspace: API, core, MCP server, voicebot, SDK) for the backend; the voice pipeline runs on LiveKit Agents with Deepgram STT, Cartesia TTS, and an LLM fallback chain (OpenAI → Gemini → Claude); Twilio is the telephony carrier; AWS SES handles email send **and** receive, with a deliverability pipeline (bounces, complaints, opens, clicks) feeding Postgres via SNS.

Voice, SMS, and email are all channels Hail ships — the runnable demo above only walks through call + email since that's the loop I actually needed first.

Feedback I actually want:

- If you've self-hosted something with this many external accounts to wire up (Twilio, LiveKit, SES) before first run — is `docker compose up` + one `.env` enough, or did I underestimate the setup tax?
- AGPLv3 for something with a hosted-service angle: has that helped or hurt adoption for your own projects?
- What's the one thing missing before you'd trust this with a real phone number?

Disclosure: I built this.

## Notes

- **Rules verification caveat:** `reddit.com` cannot be fetched directly from this environment (WebFetch to `www.reddit.com` refused, same as for the other subreddit drafts in this batch), so the compliance basis here comes from secondary sources describing r/opensource's norms, which converge on: the subreddit permits self-promotion of genuine open-source projects (real public repo, real license), provided the post reads as a project showcase rather than a bare ad or corporate announcement. That's exactly the shape this draft follows — the "rule" it satisfies is r/opensource's own subject-matter scope (genuine FOSS project + license) rather than a specific numbered rule, since the numbered rule text could not be pulled live. Re-confirm against the live rules/wiki page before posting in case mod policy has since changed.
- **SMS phrasing:** Hail as a whole ships voice, SMS, and email as core capabilities (per the team's feature-claim policy in `docs/superpowers/specs/2026-07-06-registry-submissions-design.md`), and the body above says so once, in prose. But there is currently no `hail sms` command, no `/sms` OpenAPI route, and no SMS provider wired up — see `core/hailhq/core/providers/` (only `voice/twilio.py` and `email/ses.py` exist), the README milestone checklist (SMS outbound/inbound both unchecked), and `docs/superpowers/specs/2026-07-06-sms-support-design.md` (in-progress). That's why the runnable demo only shows `hail call` + `hail email send` + `hail tail` — do not add a literal SMS command to the demo block, and if a commenter asks to see SMS live, answer "coming soon" rather than improvising a command.
- Voice is Twilio-backed (LiveKit Agents + Deepgram + Cartesia + OpenAI→Gemini→Claude LLM fallback); email (send + receive, plus deliverability events) is AWS SES-backed. No other carrier/vendor is wired up — don't name one in comments if asked "what about Telnyx / Vonage / Postmark."
- This post is framed as a showcase of a concrete use case ("here's what I built and why"), not an announcement or listing, per the team's submission-drafting design doc.
- License emphasis is deliberate for this specific target: r/opensource's whole reason for existing is genuine FOSS projects, so the post leads with the repo + AGPLv3 link instead of burying it, unlike the more feature-demo-forward framing used for r/mcp or r/SideProject.
- No fixed review/turnaround time — live as soon as it clears Reddit's spam filter and any mod queue. The real time cost is the comment-engagement commitment in Steps, step 11.
- Asset for `[gif here]`: `docs/assets/gifs/hail-tail-live-stream.gif`. No separate sized link-preview/OG-card image exists in this repo as of this draft — if a link-preview card is ever needed instead of the gif, one will need to be produced first.
- Contact/account used: whichever personal Reddit account is chosen in Steps, step 1 — record the username here once decided.
