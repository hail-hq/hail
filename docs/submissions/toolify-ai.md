---
target: "Toolify.ai"
slug: toolify-ai
category: ai-directory
url: "https://www.toolify.ai/submit"
score: 4.3
status: drafted
---

# Toolify.ai

## TODO

- [ ] Create/confirm a Toolify.ai account to submit under (form may require login before it accepts a listing).
- [ ] Decide submission tier: free queue (2-4 weeks) vs. paid **Express** ($100, faster review) vs. **Sponsor** tier — Toolify's stated preference for tools with 5K+ monthly visits is a soft signal, not a hard gate, but `hail.so`'s current traffic is unknown/unverified from this environment. If traffic is under 5K/mo, weigh whether Express is worth it to avoid a low-priority free-queue slot.
- [ ] Logo exported and ready to upload: `hail-website/public/assets/avatar-1024.png` (raster, square) — confirm Toolify's exact size/format requirement at upload time and re-export from `hail-monogram.svg` if they need a different resolution.
- [ ] Capture 4-6 screenshots — **none exist yet**, this is a real gap, not just an asset-fetch step. Hail is a CLI/API/MCP product with no traditional dashboard, so shoot: (1) the `hail.so` marketing homepage hero, (2) the `/mcp` connectors page showing client logos (Claude, ChatGPT, Cursor, etc.), (3) a terminal running `hail tail` streaming live call/SMS/email events (or use `hail/docs/assets/gifs/hail-tail-live-stream.gif` as a source frame), (4) the pricing page, (5) an MCP client (e.g. Claude.ai connector settings) mid-authorization against `mcp.hail.so`, (6) a code snippet panel showing the CLI quickstart from the README. Export all at consistent dimensions per Toolify's spec.
- [ ] Draft copy reviewed against the feature-claim policy (voice/SMS/email present-tense as shipped; provider names only where actually wired in `core/hailhq/core/providers/`).
- [ ] **Flag before publishing:** the live `hail.so` marketing site and pricing model present SMS as a shipped, billed capability (send/receive on the same number as voice), and this draft's Content follows that public stance per brief. But in this repo checkout, `core/hailhq/core/providers/` has no `sms/` module, no `send_sms` path in the CLI, SDK, OpenAPI spec, or MCP tool list (`mcp/hailhq/mcp/tools.py`) — only `voice/twilio.py` (calls) and `email/ses.py` (email) are wired end-to-end. Confirm with the team that SMS is actually live in production before this goes out; if it isn't yet, strip the SMS claim from Content and note it as coming soon instead.
- [ ] Submitted via toolify.ai/submit.
- [ ] Confirmed live — record the listing URL in Notes.

## Steps to submit

1. Go to [toolify.ai/submit](https://www.toolify.ai/submit).
2. If prompted, create an account or log in — Toolify's submission flow is typically gated behind a free account.
3. Enter the tool's website URL: `https://hail.so`.
4. Fill in **Tool Name**: `Hail`.
5. Fill in **Tagline / short description** with the one-liner from Content below: `Phone, SMS & email — for agents.`
6. Fill in **Full description** with the long copy from Content below (it's sized to the stated 200-400 word requirement — paste as-is, don't trim).
7. Select **Category** — pick the closest match Toolify exposes (likely something under "Developer Tools," "AI Agent," or "Communication" / "API" — there is no single canonical "AI Directory" category on their taxonomy, choose whichever reads closest to an agent-facing communications API).
8. Fill in **Tags/Keywords** with the list from Content below, comma-separated or one-per-box depending on the widget.
9. Set **Pricing model** — select "Free" or "Open Source" if offered (Hail is AGPLv3 and self-hostable) and add "Paid cloud option" or similar if the form allows a secondary tag, since managed Hail Cloud is also billed.
10. Upload the **logo**: `hail-website/public/assets/avatar-1024.png`.
11. Upload **screenshots** (4-6): use the set captured per the TODO item above. Order them to lead with the homepage hero, then the MCP connector view, then the terminal demo — put the most visually self-explanatory one first since Toolify surfaces the first image as the card thumbnail.
12. If there's a **contact email** field, use the address the team wants tied to this listing.
13. If there's a **GitHub / repo link** field, paste `https://github.com/hail-hq/hail`.
14. Choose the submission tier at checkout: free (2-4 week queue) or paid **Express** ($100) for faster turnaround, or a **Sponsor** tier if offered and budget allows — see TODO for the decision criteria.
15. Review the full form once before submitting — check that the description didn't get truncated by a hidden character limit.
16. Submit.
17. Note the confirmation/reference number or email Toolify sends, if any.
18. Check back per the expected turnaround (immediate-to-48h for Express, 2-4 weeks for free) and confirm the listing is live at its Toolify URL.

## Content

**Tool Name:** Hail

**Website:** `https://hail.so`

**Tagline (one-liner):** Phone, SMS & email — for agents.

**Full description (long):**

Hail gives AI agents a real phone number, SMS line, and inbox — then exposes all three as plain tool calls instead of a dashboard to click through or a webhook pipeline to wire by hand. An agent places a call, sends a text, or reads an email the same way it calls any other function.

The full surface ships four ways: a CLI for humans scripting Hail directly, a Python SDK, an OpenAPI-documented REST API, and a remote MCP server reachable over Streamable HTTP. Point any MCP-capable client — Claude, ChatGPT, Cursor, and others — at the endpoint, authorize once, and the tools are live immediately. No local install, no stdio bridge, no server to run yourself unless you want to.

Voice calls run on a real-time pipeline — Twilio for the carrier leg, with your choice of model driving the in-call conversation (OpenAI, Gemini, or Claude). Email sends and receives on AWS SES, with support for a shared Hail sending domain or your own custom domain. SMS rides the same number as voice, two-way, so an agent that just placed a call can follow up with a text without provisioning a second number.

Every action — a call placed, a message sent, an email delivered — comes back as a structured, queryable event. Tail activity live from the CLI, pull status on a specific call or message, or query delivery and engagement stats after the fact. Nothing about what the agent did is opaque.

Hail is self-hostable and open source under AGPLv3 — clone the repo, bring your own carrier and AWS credentials, and run the full stack yourself — or use the managed Hail Cloud and skip credential setup entirely. Built for agents that need to actually reach people: appointment reminders, no-show follow-ups, support callbacks, and anything else that needs a real phone number and inbox sitting behind an API instead of a human.

**Category:** ai-directory (map to Toolify's closest taxonomy match at submission time — see Steps step 7)

**Tags/keywords:** AI agents, communication API, voice calls, SMS, email API, phone calls, MCP server, self-hosted, open source, developer tools, automation, Twilio, AWS SES

**Pricing:** Open source (AGPLv3, self-host free) + managed cloud (paid, usage-based)

**Links:**

- GitHub: `https://github.com/hail-hq/hail`
- MCP endpoint: `https://mcp.hail.so`
- Docs: `https://hail.so` (site nav) / `docs/` in the repo

**Install/usage snippet:**

```bash
git clone https://github.com/hail-hq/hail
cd hail
cp .env.example .env
docker compose up

hail login                                     # device-flow auth
hail call +14155550100 --prompt "be brief"
hail email send --to a@b.com --subject hi --body "hello"
hail tail                                      # live cross-channel event stream
```

Or connect an MCP client directly to `https://mcp.hail.so` — no install required.

**Logo asset:** `hail-website/public/assets/avatar-1024.png` (raster); `hail-website/public/assets/hail-monogram.svg` if an SVG upload is accepted.

**Screenshots (4-6, see TODO — not yet captured):** homepage hero, `/mcp` connectors page, terminal running `hail tail`, pricing page, MCP client authorization flow, CLI quickstart snippet.

## Notes

- Could not fetch `https://www.toolify.ai/submit` from this environment (403) — the field list, category taxonomy, and tier pricing/turnaround above are built from the brief's stated mechanism (free 2-4wk queue / Express $100 / Sponsor) plus general knowledge of Toolify's directory format, not a verified read of the live form. Re-check exact field labels, category options, and character limits against the live page before submitting.
- Asset gap is the real blocker here, not copy: this repo has one product demo asset (`hail/docs/assets/gifs/hail-tail-live-stream.gif`) and brand marks, but zero pre-shot screenshots of the marketing site, MCP flow, or terminal in the exact framing Toolify wants. Budget time to capture these before submitting — see TODO.
- SMS claim discrepancy (see TODO): the public `hail.so` site and pricing model present SMS as shipped and billed, so Content above follows that per the brief's instruction to write core-capability claims as shipped/present-tense. But this repo's `core/hailhq/core/providers/` has no SMS-sending module and the MCP tool list has no `send_sms`/`list_sms` tool (verified against `mcp/hailhq/mcp/tools.py`, consistent with the finding recorded in `submissions/mcp-so.md` and `submissions/r-claudeai.md`). No vendor is named for SMS in Content specifically because that wiring isn't confirmed — only Twilio (voice) and AWS SES (email) are named, matching what's actually wired. Confirm SMS is live in production before this listing goes out.
- Traffic preference: Toolify's stated preference for 5K+ monthly visits is unverifiable from here — no analytics data was available in this checkout (`posthog-setup-report.md` has no visit counts). If actual traffic is well under that bar, the free queue submission may sit unprioritized; the paid Express tier sidesteps that but costs $100 — a business call, not a copy call.
- No stated point of contact for Toolify submissions beyond the form itself; treat this as self-serve.
