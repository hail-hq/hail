---
target: "Claude.ai Connectors Directory"
slug: claude-ai-connectors-directory
category: mcp-registry
url: "https://claude.com/docs/connectors/building/submission"
score: 5
status: drafted
---

# Claude.ai Connectors Directory

## TODO

- [ ] Confirm submitter has admin access to a Claude **Team or Enterprise** org — Team requires an **Owner**; Enterprise requires an Owner or a custom role with "Directory management" or "Libraries" permission. No such org confirmed yet.
- [ ] Add `title` + `readOnlyHint`/`destructiveHint` annotations to all 11 tools in `mcp/hailhq/mcp/tools.py` (currently unset — verified by grep, 2026-07-07). The portal's **Tools** step auto-syncs live tool metadata and validates these annotations are present; submission will fail this step until they're added.
- [ ] Finalize the Privacy Policy — `hail-website/content/legal/privacy.md` currently carries a "🟡 Draft pending lawyer review" banner. The **Listing** step requires a privacy policy URL; ship the reviewed version (or confirm the draft banner is acceptable to submit against) before filling that field.
- [ ] Create and provision a **reviewer test account** on Hail Cloud: verified sending identity (SES is sandboxed to verified recipients until production access is requested — see `docs/setup/aws-ses.md`) and at least one active phone number with voice+SMS capability, so a reviewer can actually exercise `place_call` / `send_email` end-to-end, not just auth.
- [ ] Write step-by-step reviewer access instructions (log in at hail.so with the test account, then click Allow on the Claude.ai OAuth consent screen) for the **Test & Launch** step.
- [ ] Confirm `https://mcp.hail.so` is reachable and still 401s + `WWW-Authenticate` correctly (last verified 2026-07-06 per the official-MCP-registry submission — recheck if stale).
- [ ] Decide and record: organization name / website / primary contact for the **Company** step (candidates below in Notes).
- [ ] Review the seven compliance acknowledgments (Content section) against actual practice before checking them in the portal.
- [ ] Draft copy reviewed against the feature-claim policy — done, see Notes.
- [ ] Submitted
- [ ] Confirmed live

## Steps to submit

This is Anthropic's guided in-product portal, not a public form — you fill it out logged into claude.ai as an admin of a Team/Enterprise org, at `claude.ai/admin-settings/directory/submissions/new`. Have the **Content** section below open in a second window; paste field-for-field.

1. **Introduction.** Read the overview screen (explains what a directory listing does/looks like). Click through.
2. **Connection.** Enter Server URL `https://mcp.hail.so`, transport type **HTTP** (Streamable HTTP — not SSE), and select the per-user OAuth connection model (each user authorizes individually; there is no shared/service credential).
3. **Tools.** The portal connects to the live server and auto-syncs the tool list (11 tools — see Content). It will flag any tool missing a `title` or a `readOnlyHint`/`destructiveHint` annotation — this **will** fail until the TODO above is fixed. Re-run this step after shipping the annotation fix.
4. **Listing.** Paste Name, Tagline, Description, Categories, Docs URL, Privacy Policy URL, Support contact, Icon, and URL slug from **Content**.
5. **Use Cases.** Paste primary use cases, prerequisites, and data direction from **Content**.
6. **Company.** Enter the organization name, website, and primary contact from **Content**.
7. **Authentication.** Select **OAuth**. No fields to type here beyond confirming the method — the OAuth flow itself is auto-discovered from the server's protected-resource metadata at `https://mcp.hail.so/.well-known/oauth-protected-resource`.
8. **Data Handling.** Select the API ownership model (first-party — Hail operates its own API in front of carrier/vendor infrastructure) and answer "no" to special data handling (no health data, no sponsored content) unless that's changed.
9. **Test & Launch.** Paste the reviewer test account credentials and the step-by-step access instructions from **Content**. Confirm you personally ran every tool against this account before checking the "I ran all tools" box.
10. **Compliance.** Check all seven acknowledgments listed under **Content** — read each one against actual Hail practice first; don't rubber-stamp.
11. **Review.** Anthropic's pre-submission checklist runs here; fix anything it flags, then submit.

After submitting, track status and any reviewer feedback in the submissions dashboard at the same `claude.ai/admin-settings/directory/submissions` area. Escalate stuck reviews to `mcp-review@anthropic.com`. Update this file's `status` field (`drafted` → `submitted` → `submitted (live)`) as it progresses.

## Content

**Name** (≤100 chars):

> Hail

**Tagline** (≤55 chars):

> Phone, SMS & email — for agents.

**Description** (≤2,000 chars):

> Hail is a self-hostable, AGPLv3 communication platform for AI agents — voice calls, SMS, and email, all reachable from one remote MCP server. No stdio, no local install: paste `https://mcp.hail.so` into Claude.ai, click Allow, and your agent gets tools to place calls, send email, read replies, and pull deliverability data — OAuth-scoped to your Hail account.
>
> Built for agents that need to actually reach people, not just draft messages: place an outbound call with an LLM-driven conversation, send transactional or outbound email, then follow up by listing inbound replies, fetching raw MIME/attachments, and pulling per-message delivery/engagement events or account-level deliverability stats. Everything is also available via CLI, Python SDK, and OpenAPI if you'd rather not go through MCP. Self-host the whole stack under AGPLv3 if you'd rather not touch Hail Cloud.

**Categories** (1–5):

> Communication, Productivity, Developer Tools

**Docs URL:**

> https://hail.so/mcp (client picker + quickstart); technical reference at the `docs/setup/mcp.md` page of the public repo (https://github.com/hail-hq/hail)

**Privacy Policy URL:**

> https://hail.so/legal/privacy — **note:** currently carries a draft banner pending lawyer review (see TODO). Confirm final version is live before submitting this field.

**Support contact:**

> hi@hail.so (only company mailbox — see `hail-website/content/legal/facts.md`)

**Icon:**

> `hail-website/public/assets/monogram-512.png` (also `monogram-1024.png` for higher-res slots) — square, transparent background, matches the icon already published in the official MCP registry `server.json`.

**URL slug:**

> hail

**Primary use cases:**

> - Place an outbound call with an LLM-driven conversation and get the transcript/outcome back.
> - Send an email and later look up whether it was delivered, opened, or bounced.
> - List and read inbound replies (including attachments) to a sent email.
> - Pull account-level deliverability stats (rates, time series) for a sending domain.

**Prerequisites:**

> A Hail account (Cloud) with at least one active phone number and one verified sending identity. No API key needed for the Claude.ai connector — OAuth handles auth.

**Data direction:**

> Both — read (list/get calls, emails, events, stats) and write (place calls, send email).

**Organization name / website / primary contact:**

> Lamona Technology AB (operates the Hail product/brand) — https://hail.so — hi@hail.so. _(Confirm this is the entity Anthropic expects here vs. a "Hail" DBA; see Notes.)_

**Tool inventory** (11 tools, code-verified from `mcp/hailhq/mcp/tools.py` — the portal auto-syncs this list live, this is for reference/cross-check only):

| Tool                   | Does                                                               |
| ---------------------- | ------------------------------------------------------------------ |
| `place_call`           | Originate an outbound phone call.                                  |
| `send_email`           | Send an outbound email.                                            |
| `get_call`             | Fetch the current state of one call.                               |
| `list_calls`           | List recent calls, cursor-paginated.                               |
| `get_email`            | Fetch one email's full record (body + inbound headers/verdicts).   |
| `list_emails`          | List emails; `direction="inbound"` for replies.                    |
| `get_email_raw`        | Presigned URL for an inbound email's raw MIME source.              |
| `get_email_attachment` | Presigned URL for one inbound attachment.                          |
| `get_email_events`     | Per-message delivery/engagement timeline (sent→delivered→opened…). |
| `get_email_stats`      | Account-level deliverability stats (rates, time series).           |
| `get_events`           | Page through the org- or call-level event stream.                  |

**Reviewer test account / access instructions** (Test & Launch step):

> Account: `<reviewer test account email>` / `<password>` — _provision before submitting, see TODO._
>
> 1. Go to https://hail.so and log in with the credentials above.
> 2. Confirm the account has an active phone number (Console → Numbers) and a verified sending identity (Console → Domains) — both pre-provisioned for this account so `place_call` and `send_email` work without further setup.
> 3. In Claude.ai, add the connector with Server URL `https://mcp.hail.so`.
> 4. When prompted, click **Allow** — this authorizes against the test account logged in above.
> 5. Exercise each of the 11 tools at least once (e.g. `place_call` to a test number, `send_email` to a verified address, then the `get_*`/`list_*` reads against the results).

**Compliance acknowledgments** (seven — review each against actual practice before checking):

1. Directory terms of service — accept as-is.
2. First-party API — Hail operates its own API/MCP server in front of carrier and vendor infrastructure (Twilio for voice/SMS, Amazon SES for email); it is not a thin passthrough wrapper of a third-party's public API.
3. No financial transactions are initiated by any tool.
4. No AI-generated media (image/video/audio) is produced or returned by any tool.
5. Prompt-injection posture: tool outputs (email bodies, call transcripts) are untrusted third-party content the calling model should treat as data, not instructions — call this out explicitly if the form has a free-text field.
6. Data collection: covered by the Privacy Policy (finalize before submitting — see TODO).
7. Public documentation: `docs/setup/mcp.md` and `hail.so/mcp` are public and cover setup end-to-end.

**Install/usage snippet** (for anything mirroring the listing outside the portal itself):

```
Server URL: https://mcp.hail.so
Auth: OAuth — click Allow when Claude.ai prompts (cloud)
      Bearer HAIL_API_KEY (self-host, not applicable to this Claude.ai listing)
```

## Notes

- **SMS is not yet an MCP tool.** `core/hailhq/core/providers/` wires up Twilio (voice + SMS capability) and Amazon SES (email) — but `mcp/hailhq/mcp/tools.py` only exposes voice and email tools today; there's no `send_sms`/`list_sms` tool yet (tracked as an approved-but-unimplemented spec, `docs/superpowers/specs/2026-07-06-sms-support-design.md`). Per the team's stated positioning call, the Description/Tagline above name SMS as a Hail capability in present tense regardless — that's a deliberate, approved brand-voice choice, not an oversight. But the **Tools** step of this portal auto-syncs the _actual_ live tool list, so SMS will correctly not appear there; don't try to force it in. Update this file (and re-sync the Tools step) once SMS tools ship.
- **Vendor breadth stays literal:** voice and SMS ride on Twilio; email rides on Amazon SES (SESv2). No other carrier/vendor is wired up in `core/hailhq/core/providers/` today — don't imply multi-carrier routing anywhere in the listing.
- **Privacy Policy is a live URL but a marked draft.** `hail.so/legal/privacy` resolves, but the content itself says "Draft pending lawyer review" as of 2026-07-07. Whether that's acceptable to submit against is a business call, not an engineering one — flagged in TODO, don't submit past it without an explicit decision.
- **Entity naming ambiguity:** the legal entity is Lamona Technology AB (Swedish AB); the product/brand is "Hail." The portal's Company step may want the operating entity, the brand, or both — confirm which before submitting rather than guessing.
- **Tool annotations are a hard blocker, not a nice-to-have.** The Tools step explicitly validates `title` + `readOnlyHint`/`destructiveHint` per tool; none of the 11 tools in `mcp/hailhq/mcp/tools.py` currently set these (verified by grep, 2026-07-07). This needs an actual code change before this submission can pass step 3, independent of anything in this doc.
- Review turnaround: Anthropic's own docs say only "review times vary with queue volume" — no fixed SLA given.
- Escalation contact for a stuck review: `mcp-review@anthropic.com`.
- This target is distinct from the **Official MCP Registry** (`docs/submissions/official-mcp-registry-modelcontextprotocol-io.md`) — that one is a self-service CLI publish (`mcp-publisher`) with no review queue; this one is a human-reviewed Anthropic-specific directory requiring a Team/Enterprise org. They can be pursued independently and in either order.
