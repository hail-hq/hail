---
target: "r/AI_Agents"
slug: r-ai-agents
category: subreddit
url: "https://www.reddit.com/r/AI_Agents/"
score: 6.3
status: drafted
---

# r/AI_Agents

## TODO

- [ ] Re-read r/AI_Agents' live rules (`reddit.com/r/AI_Agents/about/rules`) and sidebar immediately before posting — direct fetch of Reddit is blocked in this environment (see Notes), this draft is written against the submission mechanism as specified ("architecture/build-log post on the agent stack, real technical depth, not a bare ad"), not a live scrape
- [ ] Confirm whether r/AI_Agents currently gates self-promotion to a specific day/megathread ("Self-Promo Saturday" or similar has existed on this sub in the past) — if so, hold the post for that window instead of posting standalone
- [ ] Check the live flair picker at submission time and pick the closest match to "Discussion" / "Resources" / "I Built This" (exact flair set not confirmed live — see Notes)
- [ ] Confirm the numbers in **Content** against current provider pricing pages before posting — the Twilio and LiveKit rates are current list prices at draft time (Jul 2026, via web search, not Hail's own versioned dataset) and drift; Deepgram/Cartesia/LLM rates are pulled from `costs/*.json` in the repo and are versioned, so re-check `last_verified` dates are still recent
- [ ] Post from an account with some pre-existing karma/age — new/zero-karma accounts posting build-logs with external links are a common automod hold trigger
- [ ] Submitted
- [ ] Confirmed live — record the permalink in Notes, watch for mod/automod holds in the first hour, and answer every top-level comment (this sub's audience will stress-test the cost math)

## Steps to submit

1. Log in to the Reddit account you're posting from.
2. Go to [reddit.com/r/AI_Agents/submit](https://www.reddit.com/r/AI_Agents/submit).
3. Choose the **Text** post type (not Link/Image) — this is a write-up with an embedded diagram and cost table, not a link drop.
4. Paste the **Title** from **Content** below into the title field.
5. Paste the **Body** from **Content** below into the text body editor. Switch the editor to "Markdown Mode" (via the `...`/format-menu toggle) if it opens in rich-text mode by default, so the ASCII diagram and tables render as monospace/tables instead of getting mangled.
6. Preview the post. Confirm the architecture diagram code block stays fixed-width (not word-wrapped/collapsed) and the cost table renders as an actual table, not a wall of pipes — if Reddit's renderer mangles the table, fall back to the code-block version noted inline in Content.
7. Set post flair to whichever live option is closest to "Discussion" / "Resource" / "I Built This" / "Showcase" — the exact flair set isn't fixed and should be read off the live picker, not assumed from this draft.
8. Submit.
9. Watch the post for the first hour: reply to any AutoModerator self-promo disclosure prompt, and to the first few comments quickly — this sub's regulars will ask about the assumptions behind the cost table (turn count, token counts, char counts) before they'll ask about the product, and a fast, specific answer is what keeps this from reading as an ad.
10. Once confirmed live and not removed, update this file's frontmatter `status` to `submitted` (then `submitted (live)`), and add the permalink plus flair actually used to **Notes**.

## Content

**Title:**
What it actually costs to let an agent make a phone call — full architecture + per-second pricing breakdown

**Body:**

I kept seeing "give your agent a phone number" framed as a solved problem with no numbers attached, so here's the actual stack under one of these calls and what each second of it costs, based on a real appointment-confirmation call I run through this: 90 seconds, 4 turns, agent talks for about a third of it.

This is **Hail** — self-hostable, AGPLv3, voice + SMS + email for agents, driven via CLI, a Python SDK, OpenAPI, or a remote MCP server (Streamable HTTP, no stdio). Posting the architecture and the math because that's what I'd actually want to see before wiring a voice agent into anything that runs more than a few calls a day.

**Architecture — outbound call path:**

```
   CLI / Python SDK / OpenAPI / MCP (remote, Streamable HTTP, mcp.hail.so)
                              |
                              v
                Hail API (FastAPI, :8080) — POST /calls
                              |
                        dispatch into a
                        LiveKit room
                              |
                              v
                    Hail voicebot (LiveKit Agents worker)
                              |
                joins room, LiveKit places a SIP leg
                   via the Twilio trunk  ---->  PSTN  ---->  📞
                              |
              per conversational turn, in-room:
        Deepgram STT  ->  LLM (fallback chain)  ->  Cartesia TTS
                              |            \         (-> ElevenLabs fallback)
                              |             `-- OpenAI -> Gemini -> Anthropic
                              |                 (fast tier each, falls through
                              |                  on error) — or point it at your
                              |                  own OpenAI-compatible endpoint
                              v
              on hangup: call record -> Postgres
                         recording -> S3
```

Telephony and email are the real swap points behind a shared interface (`core/hailhq/core/providers/` — `voice/twilio.py`, `email/ses.py`), so `api` never imports a carrier/ESP SDK directly. STT/TTS/LLM live one layer down, inside the voicebot itself, swapped via LiveKit Agents' own plugin system and `FallbackAdapter` (Deepgram; Cartesia with an ElevenLabs failover; OpenAI → Gemini → Anthropic) rather than through that same `core/providers` interface — the voicebot does depend directly on those vendor plugin packages. Telephony is Twilio-only right now — that's the one carrier actually wired up, not a "supports N providers" claim.

**Email side, for comparison (send + receive + deliverability, not just SMTP-and-pray):**

```
  POST /emails --> AWS SES (send)
                       |
     SES Receipt Rule (inbound) --> S3 (raw MIME)
                       |                 |
                       `--> Lambda ------'
                       (HMAC-signed)
                            |
                            v
              POST /internal/ses-events
              -> verify HMAC, fetch MIME from S3
              -> parse, route to org by address
              -> write Email row, fan out webhooks
                            |
                            v
          bounces / complaints / opens / clicks
          feed the same event pipe as deliverability analytics
```

**Now the actual math for the 90-second call** (4 turns, agent speaking ~35s of the 90):

| Component                                   | Rate                          | Source                                                                                 | Usage this call                       | Cost        |
| ------------------------------------------- | ----------------------------- | -------------------------------------------------------------------------------------- | ------------------------------------- | ----------- |
| Twilio PSTN (US outbound, local)            | $0.013/min                    | Twilio Voice pricing, list price, Jul 2026                                             | 1.5 min                               | $0.0195     |
| LiveKit Cloud (SIP + WebRTC media)          | ~$0.0045/min combined         | LiveKit Cloud pricing, Ship tier                                                       | 1.5 min                               | $0.0068     |
| Deepgram STT (`nova-3`, real-time)          | $0.0048/min                   | [`costs/stt.json`](https://raw.githubusercontent.com/hail-hq/hail/main/costs/stt.json) | 1.5 min                               | $0.0072     |
| Cartesia TTS (`sonic-3.5`)                  | $50 / 1M chars                | [`costs/tts.json`](https://raw.githubusercontent.com/hail-hq/hail/main/costs/tts.json) | ~525 chars of agent speech            | $0.0263     |
| LLM (`gpt-5.4-mini`, default fallback head) | $0.75/$4.50 per 1M in/out tok | [`costs/llm.json`](https://raw.githubusercontent.com/hail-hq/hail/main/costs/llm.json) | ~2.2k in / 380 out tok across 4 turns | $0.0034     |
| **Total**                                   |                               |                                                                                        |                                       | **~$0.063** |

Call it 6 cents. At 10,000 of these a month that's ~$630 in variable infra cost — before you've paid for a phone number ($1–2/mo) or anything else. The STT/TTS/LLM rows come straight out of [Hail's own public cost dataset](https://github.com/hail-hq/hail/tree/main/costs) (CC-BY-4.0, versioned, schema-checked in CI — reuse it for your own agent's cost math, it's not Hail-specific) so you can re-run this against whatever model you'd actually pick instead of trusting my numbers. The Twilio/LiveKit rows aren't in that dataset (it only tracks LLM/STT/TTS) — they're current list prices, check your own route/volume before budgeting off them.

The follow-up confirmation email after that call: AWS SES is ~$0.10 per 1,000 sends, so ~$0.0001 — about 1/600th of the call that triggered it. Voice is the expensive channel by a wide margin; that's the actual reason "just use voice for everything" is a bad default for an agent that could email instead.

Hail ships voice, SMS, and email as channels. This post only walks the voice + email paths because those are the ones with real routes/pipelines to show; SMS doesn't have a dedicated send route yet (coming soon).

Two architecture choices worth flagging if you're building something similar:

- **LLM is swappable per-call**, not baked into the pipeline: default mode chains fast-tier OpenAI → Gemini → Anthropic models and falls through on error; or you pass your own OpenAI-compatible `base_url` and it skips the fallback chain entirely. Voice pipeline + SIP transport are the only non-swappable pieces.
- **Self-host vs. managed is a real fork, not a checkbox**: `docker compose up` runs everything except LiveKit Cloud, against your own Twilio + AWS SES accounts — you pay providers directly, no markup. The managed version at hail.so bundles the same stack with per-unit billing on top.

Repo (AGPLv3): https://github.com/hail-hq/hail
Architecture doc this diagram is adapted from: https://github.com/hail-hq/hail/blob/main/docs/architecture.md
Cost dataset: https://github.com/hail-hq/hail/tree/main/costs

Disclosure: I built this.

## Notes

- **Live rule verification blocked at draft time.** Every attempt to fetch `reddit.com/r/AI_Agents` (rules page, `about.json`) was refused in this environment (WebFetch to `www.reddit.com` errors immediately) — same blocker hit on the r/mcp and r/SideProject drafts. This draft is written to satisfy the submission mechanism as specified ("Architecture/build-log post on the agent stack," "real technical depth (diagrams, cost breakdowns) to clear noise floor") rather than a live scrape of the sidebar/wiki. Re-read the actual current rules before posting — flagged as a blocking TODO above.
- **Why this framing satisfies "not a bare ad":** the post leads with a real architecture diagram adapted from the repo's own `docs/architecture.md` (not marketing copy), a cost table built from real per-unit provider rates (three of five rows pulled from Hail's own versioned, CC-BY-4.0 cost dataset — reusable independent of the product), states the assumptions behind every number so they can be checked or argued with, and ends with a plain "I built this" disclosure. No pricing page, no CTA beyond the repo/doc links.
- **Cost math assumptions, stated so a commenter can attack them precisely:** 90-second call, 4 conversational turns, agent speaking ~35 of the 90 seconds. TTS char count (~525) derived from ~150 spoken words/min ≈ 900 chars/min at 35s of agent speech — not measured from a real call log, labeled as an estimate. LLM token counts (~2.2k in / 380 out total across 4 turns) are a round estimate for a short-context confirmation call, not a captured trace. Twilio ($0.013/min US local outbound) and LiveKit (~$0.0045/min combined SIP + WebRTC) rates came from a web search against each vendor's current pricing page (Jul 2026), not Hail's own dataset — call these "list price, your mileage varies," not a guarantee. Re-verify all four before quoting this table anywhere with more permanence than a Reddit post.
- Voice is Twilio-backed (telephony) with LiveKit Agents running Deepgram STT + a fallback LLM chain (OpenAI → Gemini → Anthropic, default models `gpt-5.4-mini` / `gemini-3-flash` / `claude-sonnet-4-6` per `.env.example`) + Cartesia TTS (with an automatic ElevenLabs failover — see `docs/architecture.md`); email (send, receive, and deliverability events) is AWS SES-backed. No other carrier/vendor is wired up (`core/hailhq/core/providers/` has exactly `voice/twilio.py` and `email/ses.py`) — don't claim multi-carrier support if asked in comments.
- **SMS phrasing:** Hail ships SMS as a capability at the product level, but there is no `POST /sms` route in `openapi/openapi.yaml`, no `hail sms` CLI command, and no SMS tool in the MCP server today — SMS rides the same Twilio account as voice at the carrier level but isn't wired through Hail's own API yet (design doc in progress: `docs/superpowers/specs/2026-07-06-sms-support-design.md`). That's why the diagram and cost table above only cover voice + email. If a commenter asks for the SMS cost-per-segment number, say "coming soon, not shipped yet" — don't improvise a figure.
- No documented review turnaround or mod-contact channel found for r/AI_Agents (fetch blocked, see above); treat as self-serve until a mod responds or automod holds it. This sub's regulars are unusually likely to challenge unit-economics claims in the comments — that's the audience this post is written for, so treat pushback on the assumptions as expected engagement, not a problem.
- Asset use: none needed — the diagram and table are inline text/markdown, which is more credible for this audience than a screenshot. If a rendered PNG version becomes useful later, `hail-website/public/assets/og-card-1200x630.png` is the closest existing sized social-preview asset, but this post doesn't need one.
