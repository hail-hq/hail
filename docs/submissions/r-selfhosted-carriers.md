---
target: "r/selfhosted (carrier comparison post)"
slug: r-selfhosted-carriers
category: subreddit
url: "https://www.reddit.com/r/selfhosted/"
score: 5.9
status: drafted
---

# r/selfhosted — carrier comparison post

Second r/selfhosted post. The first one (`r-selfhosted.md`) is the general
showcase. This one is about one feature: the agent buys the cheapest usable
phone number across carriers, and every carrier is a plug-in you configure
with a few `.env` lines.

## TODO

- [ ] Post from a personal Reddit account, not a company account
- [ ] Re-check the live rules at `reddit.com/r/selfhosted/about/rules` before posting; add the self-promotion flair the picker offers
- [ ] Wait at least two weeks after the first r/selfhosted post (`r-selfhosted.md`) went live; two self-promo posts in a row read as spam
- [ ] Replace the `$X.XX` placeholders in Content with the real numbers from one `POST /v1/numbers/quotes` call on your own stack (Twilio vs Telnyx for the same country), paste the output verbatim
- [ ] Confirm `docs/public/self-host/telnyx.md` and `didww.md` are live on `hail.so/docs` at the linked paths
- [ ] Submitted
- [ ] Confirmed live — record the permalink in Notes and answer every comment for 48 hours

## Steps to submit

1. Log into the personal Reddit account.
2. Go to [reddit.com/r/selfhosted](https://www.reddit.com/r/selfhosted/) → **Create Post** → **Text**.
3. Paste the **Title** and **Body** from Content. Switch the editor to Markdown mode so the code blocks render.
4. Pick the self-promotion / release flair the live picker offers.
5. Preview: the two code blocks and the three GitHub links must render.
6. Post. Reply to every comment for 48 hours. Expect: "why not SIP only?", "can I use my own carrier?", "what runs locally?" — answers are in Notes.
7. Set `status: submitted` in this file's frontmatter and add the permalink to Notes.

## Content

**Title:**
My self-hosted AI agent now shops for its own phone number across carriers (Twilio / Telnyx / DIDWW) — how the carrier plug-ins work

**Body:**

I run [Hail](https://github.com/hail-hq/hail) (AGPLv3, docker compose) so my agents can make calls and send SMS/email. Until this week it was Twilio only. Now a number purchase looks like this:

```bash
hail numbers acquire --country PT
```

Behind that one command the API asks every carrier you configured for live inventory, the real monthly and setup price, and what the regulator wants from you before you can hold that number. It ranks the offers: ready-to-buy first, then least paperwork, then cheapest month, then cheapest setup. You can pin a carrier with `--provider telnyx`, or pass the exact offer with `--quote-id`. Same thing over HTTP: `POST /v1/numbers/quotes`, then `POST /v1/numbers` with the `quote_id`.

Example from my box, same country and number type: `$X.XX/mo` on one carrier vs `$X.XX/mo + $X.XX setup` on the other, one of them flagged "verification required" (paste your own quote output here).

**What's self-hosted vs external.** The API, the carrier logic, the billing ledger, the order reconciler and the MCP server are containers on your machine. The carriers are accounts you own: Twilio, Telnyx, DIDWW (DIDWW is outbound calls only for now; you add its numbers by hand). Media still goes through LiveKit Cloud. Nothing about your numbers or orders leaves your database except the carrier API calls themselves.

**Adding a carrier is a few `.env` lines.** Each carrier is one entry in a registry (`core/hailhq/core/carrier_routing.py`): which LiveKit SIP trunk to dial through, how to send SMS, whether orders complete asynchronously. Telnyx needs five values and about ten minutes: an outbound voice profile, a SIP connection, a LiveKit trunk pointed at `sip.telnyx.com`, and the webhook public key. Docs: [Telnyx](https://github.com/hail-hq/hail/blob/main/docs/public/self-host/telnyx.md), [DIDWW](https://github.com/hail-hq/hail/blob/main/docs/public/self-host/didww.md).

```bash
LIVEKIT_TWILIO_SIP_OUTBOUND_TRUNK_ID=ST_...
LIVEKIT_TELNYX_SIP_OUTBOUND_TRUNK_ID=ST_...
LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID=ST_...
TELNYX_API_KEY=...
TELNYX_CONNECTION_ID=...
TELNYX_SIP_USERNAME=...
TELNYX_PUBLIC_KEY=...
```

**The part that took the longest: not losing money on a half-finished order.** Telnyx orders are asynchronous. The API reserves setup plus the first month from your credit ledger, commits the pending number, and only then places the order. A sweeper polls the carrier; success turns the reservation into the monthly fee, a definite failure refunds it once, and an order the carrier never confirms is failed after two hours and flagged for you to check by hand. A retry never places a second paid order, and a number never silently moves to another carrier.

**Honest limits.** Inbound calls are not there yet. Some countries need you verified with the carrier before you can buy (UK mobile, for example); Hail collects the documents and a plug-in files them with the carrier, but only Twilio has that plug-in today, so a Telnyx offer in such a country shows "verification required" until you approve a requirement group in their portal. Prices are what the carrier returns at quote time, in USD only.

Repo: [github.com/hail-hq/hail](https://github.com/hail-hq/hail). I'm the developer. What carrier would you want next, and does "the agent picks the cheapest ready number" match how you'd want this to behave, or would you rather always choose by hand?

## Notes

- **Feature claims** match `CHANGELOG.md` 0.24.0: Telnyx numbers, calls and SMS; DIDWW outbound calls only; verification plug-in Twilio only; quotes in USD. Do not claim inbound calls, Telnyx verification, or self-hosted media.
- **Likely questions.** "Why LiveKit Cloud?" — media SFU, self-hosting it is on the roadmap, not shipped. "Can I bring carrier X?" — one registry entry plus provider adapters in `core/hailhq/core/providers/`; link the DIDWW PR (hail-hq/hail#116) as the smallest example. "Does the agent spend my money on its own?" — only from prepaid credits, only after a quote it was given, one order per quote, refunds on definite failure.
- **Do not** post the two r/selfhosted drafts within the same two weeks.
- Asset: none required. A screenshot of the quotes JSON is optional.
