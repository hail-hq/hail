---
target: "AlternativeTo"
slug: alternativeto
category: dev-directory
url: "https://alternativeto.net/faq/"
score: 5
status: drafted
---

# AlternativeTo

## TODO

- [ ] Confirm an AlternativeTo account exists that is **at least 1 week old** — new accounts cannot submit ("Suggest new application" is gated on account age, per the FAQ). If starting from zero, create the account first and wait out the week before attempting anything else on this list.
- [ ] Capture real screenshots of an actual product surface before submitting — AlternativeTo explicitly rejects thin "wrapper" listings, and today the only visual asset in this repo is `docs/assets/gifs/hail-tail-live-stream.gif`. Capture at minimum: (1) `hail call` + `hail email send` CLI output, (2) `hail tail` cross-channel event stream, (3) the OpenAPI docs page (`openapi/openapi.yaml` rendered, e.g. via Swagger UI) or the MCP client-picker page at hail.so/mcp. Do not submit with zero screenshots — see Notes.
- [ ] Icon asset ready: squared PNG or SVG, 280x280px or larger, transparent background. Candidates: `hail-website/public/assets/hail-monogram.svg` or `hail-website/public/assets/monogram-512.png` / `monogram-1024.png` — confirm transparency on the raster PNGs before uploading (SVG is the safer bet).
- [ ] Draft copy reviewed against the feature-claim policy (SMS/vendor accuracy — see Notes)
- [ ] Decide **Platforms** picker selections against the live form (Self-Hosted, Web, API/Linux/Mac/Windows via Docker — exact option set unconfirmed, see Notes)
- [ ] Decide **License** field: `Open Source` (AGPLv3)
- [ ] Submitted via the "Suggest new application" form
- [ ] Confirmed live — record listing URL in Notes

## Steps to submit

1. If no AlternativeTo account exists yet, go to [alternativeto.net](https://alternativeto.net) and create one now — then wait at least 7 days before continuing (new-account submissions are blocked for the first week).
2. Log in. Click the user icon in the top-right corner, then choose **"Suggest new application."**
3. **Application name:** paste `Hail`.
4. **Website / URL:** paste `https://hail.so`.
5. **Platforms:** select the closest matches on offer — likely `Self-Hosted`, `Web`, `Linux`, `Mac`, `Windows` (all reachable via the Docker Compose self-host path or the CLI), and `API` if that option exists. Pick whatever the live picker actually offers; this list is inferred, not confirmed against the current form.
6. **License:** select `Open Source`.
7. Paste the **description** from **Content** below into the description field.
8. Add the **tags** from **Content** below into the tags field.
9. Upload the **icon** asset (see TODO — squared, transparent, 280x280+).
10. Upload the **screenshots** captured per the TODO above. Do not proceed with an empty or single-asset gallery — see Notes on why this target rejects thin listings.
11. Click **"Submit the application."**
12. Note the submission date in **Notes**, then update this file's frontmatter `status` to `submitted`.
13. Check back within a couple of days to a week (stated review window). Once the listing is live, record the listing URL in **Notes** and update `status` to `submitted` (add a "(live)" note).

## Content

**Application name:** Hail

**Website:** `https://hail.so`

**Repo:** `https://github.com/hail-hq/hail`

**One-liner:** Phone, SMS & email — for agents.

**Description (long):**
Hail is a self-hostable, AGPLv3 communication platform built for AI agents. It gives an agent a real phone number, inbox, and messaging line: place and receive voice calls, send and receive SMS, send and receive email, and read back structured events and per-channel analytics — all through one system, not a pile of glue code around someone else's API. Run the whole stack yourself with `docker compose up` on your own Twilio and AWS SES accounts — Postgres-backed, with deliverability tracking and analytics built into the core, not bolted on after the fact. Agents drive it directly: a CLI (`hail call`, `hail email send`, `hail tail` for a live cross-channel event stream), a Python SDK, a documented OpenAPI spec, or a remote MCP server over Streamable HTTP — no stdio, nothing to install locally, just point a client at the endpoint and authorize. Full source is available under AGPLv3; nothing about the core communication surface is held back behind a paid tier.

**License:** Open Source — AGPL-3.0-or-later ([`LICENSE`](../../LICENSE))

**Platforms:** Self-Hosted, Web, Linux, Mac, Windows (via Docker Compose / CLI) — confirm exact picker options at submission time

**Tags/keywords:** communication, voice calls, sms, email, ai agents, developer tools, self-hosted, open-source, api, mcp, cli

**Install / usage snippet (for the description body or a "how it works" field, if offered):**

```bash
# Self-host
git clone https://github.com/hail-hq/hail
cd hail && cp .env.example .env   # Twilio, LiveKit Cloud, Deepgram, Cartesia, AWS SES, one of OpenAI/Gemini/Anthropic
docker compose up

# CLI
hail call +14155550100 --prompt "be brief"
hail email send --to a@b.com --subject hi --body "hello"
hail tail                          # cross-channel live event stream

# Python SDK
pip install hail-sdk

# MCP (Streamable HTTP, no stdio) — self-hosted today
hail mcp endpoint                  # prints your self-hosted connector URL
```

**Icon asset:** `hail-website/public/assets/hail-monogram.svg` (transparent square mark; `hail-website/public/assets/monogram-512.png` or `monogram-1024.png` as raster fallback — verify transparency before upload)

**Screenshots (capture before submitting — see TODO, none exist yet beyond the gif below):**

1. `docs/assets/gifs/hail-tail-live-stream.gif` — animated terminal demo of `hail tail` streaming live call/SMS/email events across channels. Only real product asset that exists in this repo today.
2. _(to capture)_ `hail call` + `hail email send` CLI output — shows the CLI actually placing a call / sending mail, not just a help screen.
3. _(to capture)_ Rendered OpenAPI docs (from `openapi/openapi.yaml`) or the MCP client-picker page at hail.so/mcp — establishes the API/MCP surface as real, not a thin wrapper.

**Contact email (if requested):** `hi@hail.so`

## Notes

- **Account-age gate:** the FAQ states new accounts must wait one week after creation before they can submit via "Suggest new application." Plan the account creation date accordingly; don't attempt submission before the week is up.
- **Review turnaround:** per the FAQ, "usually a couple of days and up to a week," varying with submission volume. No hard SLA.
- **This target explicitly rejects "AI wrappers," simple converters/calculators, and clone scripts**, and requires the listing to show real value and substance rather than something "indistinguishable from what's already widely available." The description above leans on concrete, verifiable substance — self-hosted infra, own Postgres-backed analytics/deliverability tracking, CLI/SDK/OpenAPI/MCP surfaces — specifically to avoid reading as a thin wrapper around a third-party API. Do not water this down to a generic "AI-powered communication tool" pitch.
- **Screenshots are a hard requirement in practice, not optional** given the anti-wrapper policy: a listing with no visual evidence of an actual product surface is a bad candidate for approval here. Do not submit until at least the CLI-output and API/MCP screenshots exist (see TODO) — the single existing gif is a good start but is not enough on its own.
- **Icon spec:** "a squared PNG or SVG image, 280x280 or bigger, with transparent background is suggested" per the FAQ. `hail-website/public/assets/monogram-1024.png` and `monogram-512.png` meet the size bar; confirm transparency, or use the SVG mark to be safe.
- Voice is Twilio-backed, email (send + receive) is AWS SES-backed — see `core/hailhq/core/providers/voice/twilio.py` and `core/hailhq/core/providers/email/ses.py`. No other carrier/vendor is wired up; don't name any other provider in the listing.
- SMS is a shipped, present-tense capability of Hail as a whole (voice, SMS, email) per product copy, but there is no SMS provider adapter in `core/hailhq/core/providers/` yet and no SMS tool in the MCP server (`mcp/hailhq/mcp/tools.py` exposes only call and email tools plus event/stats readers). Keep the description's SMS claim at the product level (fine, since Hail overall ships SMS per brand-voice policy); don't demo a literal `hail sms` command or name an SMS carrier — there isn't one wired up yet.
- **Corrected 2026-07-07: `https://mcp.hail.so` (Hail Cloud) IS live today**, not "coming soon" as the root `README.md`'s quickstart comment states — that comment is stale. Verified directly: `curl -i https://mcp.hail.so/` returns `401` with `WWW-Authenticate: Bearer ... resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"`, i.e. a real, working OAuth-protected endpoint, matching `docs/setup/mcp.md` and the `oauth-rs` mode actually implemented in `mcp/hailhq/mcp/auth.py`. Self-host (`hail mcp endpoint` after `docker compose up`) is also real and valid — both are live options, not one-live-one-future.
- Submission mechanics above (account-icon field labels, Platforms/License picker options, exact form layout) come from the public FAQ page (`alternativeto.net/faq/`) fetched for this draft, not from walking the live "Suggest new application" form itself — confirm exact field names/options once logged in and adjust which **Content** items map to which field.
- Contact used: `hi@hail.so` (or the founder's AlternativeTo account, once created).
