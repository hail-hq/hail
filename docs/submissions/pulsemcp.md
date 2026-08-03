---
target: "PulseMCP"
slug: pulsemcp
category: mcp-registry
url: "https://www.pulsemcp.com/submit"
score: 9
status: drafted
---

# PulseMCP

## TODO

- [x] Confirm Hail's `server.json` is published and live on the Official MCP Registry — **done 2026-07-07**: `io.github.hail-hq/hail-mcp` v0.1.0, `status: active` (see `docs/submissions/official-mcp-registry-modelcontextprotocol-io.md`). This was the actual prerequisite; PulseMCP has no independent submission of its own.
- [x] No PulseMCP account needed — automatic ingestion path requires no login
- [x] No assets to attach here — icons/description are pulled from the `server.json` manifest already published to the Official Registry
- [ ] **Waiting on PulseMCP's weekly ingestion pass** (published 2026-07-07; check back ~2026-07-14). Not actionable until then — nothing left for us to do.
- [ ] Confirmed live: search `pulsemcp.com` for "Hail" / `hail-mcp` (checked 2026-07-07, too soon — page returned 403 to an automated fetch anyway, re-check manually in a browser)
- [ ] If not live after a week, fall back to the manual URL-submit form (Steps §2) or email `hello@pulsemcp.com`

## Steps to submit

PulseMCP has no standalone submission form for this listing. Per its own submit page: _"We ingest entries from the Official MCP Registry daily and process them weekly."_ The real work is publishing to the Official Registry — do that first (see `docs/submissions/official-mcp-registry-modelcontextprotocol-io.md`), then:

1. Do nothing for up to a week. PulseMCP's crawler picks up new/updated Official Registry entries automatically — no separate copy of the description, tags, or icons to maintain here.
2. After ~1 week, go to `https://www.pulsemcp.com` and search "Hail" (or "hail-mcp") to confirm the listing appeared and rendered correctly (name, description, icon, GitHub link, remote URL).
3. **If it hasn't appeared after a week**, use the manual fallback form at [pulsemcp.com/submit](https://www.pulsemcp.com/submit): paste the GitHub repo URL `https://github.com/hail-hq/hail` (or the `mcp` subfolder URL, `https://github.com/hail-hq/hail/tree/main/mcp`, if the form prefers a scoped path) into the single URL field and submit.
4. **If the listing is live but wrong/stale** (e.g. after a `server.json` update), don't resubmit — email `hello@pulsemcp.com` directly and ask for an out-of-cycle refresh, per the submit page's own guidance.
5. Once confirmed live, flip `status: submitted` (then `submitted (live)`) in this file's frontmatter and in `docs/submissions/README.md`.

## Content

Nothing to paste into a PulseMCP-specific field — there is none. The listing is generated from the same `server.json` already drafted for the Official Registry. Reference copy, kept here so the rendered PulseMCP listing can be sanity-checked against it (§Steps 2):

**One-liner:**

> Phone, SMS & email — for agents. One remote MCP endpoint, OAuth login, zero install.

**Description** (mirrors the `server.json` `description` field the crawler ingests):

> Voice calls, SMS, and email for AI agents — one remote MCP endpoint, OAuth login, zero install. Place calls, send messages, read replies, and pull deliverability events without standing up a phone system or mail server.

**Repo / manual-fallback URL:**

```
https://github.com/hail-hq/hail
```

**Remote server URL (what should render on the listing):**

```
https://mcp.hail.so
```

**Icon** (pulled from `server.json`, mirrored at):

```
hail-website/public/assets/monogram-512.png
hail-website/public/assets/monogram-1024.png
```

**Tool inventory** (11 tools, for cross-checking the listing's tool list against `mcp/hailhq/mcp/tools.py`):

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

## Notes

- **This target has no independent submission mechanism** — per the task's own framing, it's "same as Official Registry; no separate work." Do not draft or maintain separate copy for PulseMCP; keeping `server.json` accurate on the Official Registry is sufficient. This file exists mainly as a tracking/verification checkpoint, not a drafting task.
- **Review turnaround:** ingestion runs daily, processed weekly, per PulseMCP's own submit page. Allow up to a week before treating a missing listing as a problem.
- **Contact for out-of-cycle fixes:** `hello@pulsemcp.com`, per the submit page ("If it has been a week since you published there, or want to make other adjustments to your listing on pulsemcp.com, please email us...").
- **SMS caveat carries over from the Official Registry listing:** `core/hailhq/core/providers/` wires up voice and email only today; SMS is spec'd (`docs/superpowers/specs/2026-07-06-sms-support-design.md`) but not yet an MCP tool. The one-liner/description name SMS as a Hail capability per the team's positioning call (core-capability claims are present-tense regardless of milestone state), but the tool inventory above lists only what's callable today. Whatever PulseMCP ingests will reflect the Official Registry's `server.json` as of publish time — no separate correction needed here, just keep the source manifest current.
- **Ordering dependency:** this submission is blocked on `docs/submissions/official-mcp-registry-modelcontextprotocol-io.md` reaching `status: submitted` first — there is nothing to do here until that one ships.

```

File checked: `/Users/r/playground/hail/docs/submissions/pulsemcp.md` (no edits made — already accurate).
```
