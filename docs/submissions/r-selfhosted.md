---
target: "r/selfhosted"
slug: r-selfhosted
category: subreddit
url: "https://www.reddit.com/r/selfhosted/"
score: 5.9
status: drafted
---

# r/selfhosted

## TODO

- [ ] Post from a personal Reddit account, not a company/bot account
- [ ] Re-check the live sidebar/wiki rules at `reddit.com/r/selfhosted/about/rules` immediately before posting — this draft's compliance basis was reconstructed from secondary sources (direct fetch of `reddit.com` is blocked from this environment, see Notes), not the primary rules page
- [ ] Confirm the affiliation-disclosure line (Content, end of Body) matches whatever exact wording/placement r/selfhosted's live self-promotion rule asks for (e.g. some variants want it as a post flair, not just prose — check the flair picker in step 6 below)
- [ ] Draft copy reviewed against the feature-claim policy (SMS wording, LiveKit-Cloud dependency wording — see Notes)
- [ ] Confirm `LICENSE` in the repo root still reads AGPL-3.0 verbatim before quoting it
- [ ] Confirm the docker-compose snippet in Content still matches `docker-compose.yml` / `docker-compose.local.yml` / `docker-compose.prod.yml` at post time (paste fresh from repo if any have changed)
- [ ] Re-check `ghcr.io/hail-hq/hail-*` package visibility right before posting (`https://github.com/hail-hq/hail/pkgs/container/hail-api`) — as of this draft the packages 404 anonymously (private/unpublished, matching `docs/operations.md`'s "Service images ... are not published"), so the prod-overlay caveat in Content is load-bearing; drop it only if that's changed
- [ ] Capture/attach the demo asset (`docs/assets/gifs/hail-tail-live-stream.gif`) if the composer supports inline media on a text post; otherwise post text-only
- [ ] Pick the closest post flair at submission time (e.g. "Release" / "Self-Promotion" / "Guide" — exact flair list can drift, read it off the live picker)
- [ ] Submitted
- [ ] Confirmed live — record thread permalink in Notes, and commit to answering every comment for the first 24–48 hours (r/selfhosted especially will ask about resource footprint and external-dependency honesty)

## Steps to submit

1. Log into the personal Reddit account that will post.
2. Go to [reddit.com/r/selfhosted](https://www.reddit.com/r/selfhosted/) and click **Create Post**.
3. Select the **Text** post type (not "Link") — this is a setup-guide-and-showcase post, not a bare link drop.
4. Paste the **Title** from Content into the title field.
5. Paste the **Body** from Content into the post body (markdown editor; switch to "Markdown Mode" via the editor's `...`/format toggle if it opens in rich-text mode, so the code fences render correctly).
6. Set the post flair to whichever option in the live flair picker is closest to "Release" / "Self-Promotion" / "Guide" — r/selfhosted requires self-promo posts to be clearly flagged as such; use whatever the live picker offers.
7. If the composer lets you drop an image/gif inline inside a text post's body, insert `docs/assets/gifs/hail-tail-live-stream.gif` at the point in the body marked `[gif here]`. If the editor forces media into a separate gallery/link post type, skip this — do not convert the post to an image/gallery post.
8. Preview the post; confirm the code blocks (docker-compose command, `.env` steps, CLI commands) render as code blocks, and the `github.com/hail-hq/hail` link is clickable.
9. Proofread once more against the SMS and LiveKit-Cloud phrasing notes in **Notes** below before posting.
10. Click **Post**.
11. For the first 24–48 hours, check back and reply to every comment — r/selfhosted regulars will specifically probe "what's actually running on my box vs. what's still a cloud dependency" (Twilio, LiveKit Cloud, AWS SES); answer those directly and don't downplay them.
12. Update this file's frontmatter `status` to `submitted`. Once the thread has stabilized, add the permalink and a one-line summary of the feedback received to **Notes**.

## Content

**Title:**
Self-hosted phone/email gateway for AI agents — docker-compose, AGPLv3 (I'm the dev)

**Body:**

What it is: **Hail**, a self-hostable communication platform for AI agents — phone calls and email today (SMS is scaffolded but not wired up yet, see below), driven by a CLI, a Python SDK, a documented OpenAPI spec, or a remote MCP server (Streamable HTTP — no stdio package to install locally, the server just runs as another container in the stack). Real repo, real license: [github.com/hail-hq/hail](https://github.com/hail-hq/hail), [AGPL-3.0-or-later](https://github.com/hail-hq/hail/blob/main/LICENSE) — modify it and run it as a service for others, and you owe your source back.

The use case I built it for: an agent that takes a call, then follows up by email the second the call ends, with one log I can tail instead of three provider dashboards. That loop, running against my own stack:

```bash
hail call +14155550100 --prompt "confirm the demo time, keep it under 90 seconds"
hail email send --to lead@example.com --subject "Following up on our call" \
  --body "Great chatting — sending the deck now."
hail tail
```

`hail tail` is a cross-channel event stream — `queued → dialing → in_progress → completed` for the call, `sent → delivered` for the email, merged into one terminal, no dashboard tab-switching.

[gif here]

**Setup — this is the part for this sub.** Everything except the two things below runs on your own box:

```bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env
# fill in Twilio, LiveKit Cloud, Deepgram, Cartesia, and one of OpenAI / Gemini / Anthropic
docker compose up
```

That base `docker-compose.yml` brings up four services: `api` (FastAPI), `voicebot` (the LiveKit Agents worker that runs the voice pipeline), `mcp` (the Streamable HTTP MCP server, port 8081), and `minio` (S3-compatible storage). Two overlays on top, pick the one that fits:

- `docker-compose.local.yml` — adds a bundled Postgres container, builds all four services from source, for a fully self-contained dev/homelab box. This is the one that works with a straight `git clone` + `up`, no forking required.
- `docker-compose.prod.yml` — points at `ghcr.io/hail-hq/hail-*` image tags instead of building from source, rebinds ports to `127.0.0.1`, and adds Caddy as the TLS edge with auto Let's Encrypt — the config I run for my own single-VM deploy (walkthrough: [docs/setup/vm-deploy.md](https://github.com/hail-hq/hail/blob/main/docs/setup/vm-deploy.md)). Heads up: my `ghcr.io/hail-hq/hail-*` packages are private right now, so this overlay isn't a drop-in pull from my repo — fork it and point your own GitHub Actions/registry login at your fork's images (vm-deploy.md covers this), or just build from source with `docker compose -f docker-compose.yml -f docker-compose.prod.yml build`.

```bash
# fully self-contained (bundled Postgres, builds from source):
docker compose -f docker-compose.yml -f docker-compose.local.yml up

# single-VM with your own domain + TLS, managed Postgres (after fork+build/push,
# or `build` instead of `pull` if you're not using a registry at all):
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d
```

Then generate a shared API key and seed a phone number — see [docs/operations.md](https://github.com/hail-hq/hail/blob/main/docs/operations.md) → "Self-host: first-run setup" for the exact commands. Export `HAIL_API_KEY` and the CLI/API/MCP calls above work against your own stack, no managed-cloud account involved.

**Honest disclosure on external dependencies**, since this sub calls it out fast if you don't: the orchestration layer (API, voice pipeline logic, MCP server, event log, deliverability tracking) is entirely yours to self-host, but voice calls still need a telephony carrier (Twilio) and a real-time media SFU (LiveKit Cloud — self-hosting the SFU is on the roadmap, not shipped yet), and email send/receive goes through AWS SES. You're not eliminating those accounts, you're eliminating the SaaS layer sitting on top of them and owning the data/logic in between.

Stack: Go CLI; Python (`uv` workspace — API, core, MCP server, voicebot, SDK) backend; voice pipeline on LiveKit Agents with Deepgram STT, Cartesia TTS (ElevenLabs as automatic fallback if `ELEVEN_API_KEY` is set), Silero VAD, LiveKit's turn-detector, and an LLM fallback chain (OpenAI → Gemini → Claude) or bring your own OpenAI-compatible endpoint; Twilio for telephony; AWS SES for email send **and** receive, with bounce/complaint/open/click tracking flowing into Postgres via SNS.

Feedback I actually want from this crowd specifically:

- Is `docker compose up` + one `.env` an honest "self-hosted" bar for you, given the external carrier/SFU accounts above are unavoidable for a working phone line — or does that disqualify it from how you'd describe "self-hosted" here?
- Resource footprint on a small VM/homebox — anyone want a docker-stats readout for the full stack (api + voicebot + mcp + minio + postgres)?
- AGPLv3 specifically for something with a hosted-cloud sibling offering (hail.so) — does that combination read as genuinely open to you, or as open-core-with-extra-steps?

Disclosure: I'm the developer, this is my project.

## Notes

- **Rules verification caveat:** `reddit.com` cannot be fetched directly from this environment (WebFetch to `www.reddit.com` refused, consistent with the other subreddit drafts in this batch), so the compliance basis here comes from secondary sources describing r/selfhosted's norms: self-promotion of a genuinely self-hostable tool is allowed provided the OP discloses their affiliation with the project. Both conditions are satisfied by construction (Hail runs via `docker compose up` end-to-end for the orchestration layer, and the post ends with an explicit "I'm the developer" line) — re-confirm exact rule number/wording and any flair requirement against the live rules page immediately before posting.
- **SMS phrasing:** per the team's feature-claim policy (`docs/superpowers/specs/2026-07-06-registry-submissions-design.md`), core capabilities ship as present-tense claims once wired up — SMS is not yet wired up. There's no `hail sms` command, no SMS OpenAPI route, and no SMS provider adapter (`core/hailhq/core/providers/` only has `voice/twilio.py` and `email/ses.py`; see also the README milestone checklist, SMS outbound/inbound both unchecked, and the in-progress `docs/superpowers/specs/2026-07-06-sms-support-design.md`). The draft above states this plainly ("SMS is scaffolded but not wired up yet") instead of implying it's live — keep that framing if edited, and answer "coming soon" if asked in comments.
- **LiveKit-Cloud dependency phrasing:** README's Infrastructure milestones list "Self-hosted LiveKit SFU — docker compose integration" as unchecked. Voice calls currently require LiveKit Cloud as an external dependency; the draft discloses this explicitly rather than letting "self-hostable" imply the whole call path runs locally. Do not remove or soften that paragraph — r/selfhosted is exactly the audience that will catch a glossed-over cloud dependency and call it out in the top comment.
- Voice is Twilio-backed (LiveKit Agents + Deepgram STT + Cartesia TTS with an ElevenLabs fallback (`voicebot/hailhq/voicebot/pipeline.py`, `ELEVEN_API_KEY` in `.env.example`) + OpenAI→Gemini→Claude LLM fallback); email (send + receive, plus deliverability events) is AWS SES-backed. No other carrier/vendor is wired up — don't name Telnyx/Vonage/Postmark/Whisper/AssemblyAI/etc. as live if asked.
- **GHCR image publish status:** `docker-compose.prod.yml` references `ghcr.io/hail-hq/hail-api:latest` / `-voicebot:latest` / `-mcp:latest`, and `.github/workflows/deploy.yml` does build+push those tags on every merge to `main` — but `docs/operations.md` states plainly that these service images "are not published" (publishing is a v1.x item), and anonymously hitting `github.com/hail-hq/hail/pkgs/container/hail-api` 404s, consistent with GitHub's default of private visibility for CI-pushed packages. `docs/setup/vm-deploy.md` confirms this is expected — its own footgun table lists `docker pull` 401/"denied" from GHCR as normal, fixed by either making the package public or logging in with a PAT against _your own_ fork's images. Content's `docker-compose.prod.yml` paragraph and command block were corrected to say this explicitly (fork + build/push your own images, or `build` instead of `pull`) rather than implying a stranger can `docker compose ... up -d` straight against my repo's images — leaving the original phrasing in would have handed r/selfhosted a guaranteed "your compose file doesn't work as written" top comment.
- This post is framed as a self-hosted setup showcase ("here's what I built, here's exactly how to run it, here's what's genuinely local vs. still a cloud account"), not an announcement or listing, per the team's submission-drafting design doc — and it foregrounds the docker-compose file/overlay structure specifically because that's what this mechanism and this audience want up front, unlike the more narrative framing used for r/opensource.
- No fixed review/turnaround time — live as soon as it clears Reddit's spam filter and any mod queue. The real cost is the comment-engagement commitment in Steps, step 11.
- Asset for `[gif here]`: `docs/assets/gifs/hail-tail-live-stream.gif`. No separate sized link-preview/OG-card image exists in this repo as of this draft.
- Contact/account used: whichever personal Reddit account is chosen in Steps, step 1 — record the username here once decided.
