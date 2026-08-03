---
target: "awesome-mcp-servers (punkpeye/awesome-mcp-servers)"
slug: awesome-mcp-servers-punkpeye-awesome-mcp-servers
category: github-list
url: "https://github.com/punkpeye/awesome-mcp-servers"
score: 9.5
status: submitted
---

# awesome-mcp-servers (punkpeye/awesome-mcp-servers)

## TODO

- [x] GitHub account with a fork of `punkpeye/awesome-mcp-servers` (no other account/login needed — plain PR flow)
- [x] Confirm `github.com/hail-hq/hail` is public (it is)
- [x] Draft copy reviewed against the feature-claim policy (SMS is a shipped product capability, not yet an MCP _tool_ — kept out of the tool-facing wording; only Twilio/SES named as vendors)
- [x] Re-check insertion point immediately before editing — the list churns fast; confirm the entry still belongs between `gotoolkits/wecombot` and `hannesrudolph/imessage-query-fastmcp-mcp-server` in the **Communication** section (alphabetical by `owner/repo`, case-insensitive: `hail-hq` < `hannesrudolph`)
- [x] No image/logo asset needed — this target is a plain one-line Markdown list entry, text only
- [x] Branch created, single line added, committed
- [x] PR opened against `punkpeye/awesome-mcp-servers` main, using the title convention from Steps §7
- [ ] PR merged
- [ ] Confirmed live: line appears in `README.md` on `main`, record the PR URL in Notes

## Steps to submit

1. Fork [`punkpeye/awesome-mcp-servers`](https://github.com/punkpeye/awesome-mcp-servers) (click **Fork**, top right of the GitHub page).
2. Clone your fork locally and create a branch: `git checkout -b add-hail-mcp-server`.
3. Open `README.md` and jump to the `### 💬 <a name="communication"></a>Communication` section.
4. Find the line for `gotoolkits/wecombot` and the line right after it for `hannesrudolph/imessage-query-fastmcp-mcp-server`. Insert a new line **between** them (alphabetical order within the section, by `owner/repo`):

   ```markdown
   - [hail-hq/hail](https://github.com/hail-hq/hail) 🐍 ☁️ - Phone, SMS & email for AI agents. One remote MCP server (Streamable HTTP, OAuth or API-key auth, no local install) exposing call, email, and event tools; also usable via CLI, Python SDK, and OpenAPI. Self-hostable, AGPLv3.
   ```

   The two legend icons follow the repo's own key (see `## Legend` in the target README): 🐍 = Python codebase (the MCP service and shared core are Python), ☁️ = Cloud Service (it talks to remote carrier/mail APIs, not local software).

5. Save. Diff should be a single added line — nothing else in `README.md` touched.
6. Commit: `git commit -m "Add hail-hq/hail to Communication"`, then push: `git push origin add-hail-mcp-server`.
7. Open a PR from your fork's branch into `punkpeye/awesome-mcp-servers:main`. Title: `Add hail-hq/hail to Communication`. Body: one or two sentences — what Hail is and why it fits Communication (voice/SMS/email MCP server for agents). Per the target's own `CONTRIBUTING.md`, if you are an automated agent opening this PR, append `🤖🤖🤖` to the title to opt into their fast-tracked agent-PR merge lane.
8. Wait for maintainer review (see Notes for turnaround expectations).
9. Once merged, confirm the line is live on `main`, then flip this file's frontmatter `status` to `submitted` and log the merged PR URL in Notes.

## Content

**Exact line to add** (Communication section, alphabetical order):

```markdown
- [hail-hq/hail](https://github.com/hail-hq/hail) 🐍 ☁️ - Phone, SMS & email for AI agents. One remote MCP server (Streamable HTTP, OAuth or API-key auth, no local install) exposing call, email, and event tools; also usable via CLI, Python SDK, and OpenAPI. Self-hostable, AGPLv3.
```

**Repo link:** `https://github.com/hail-hq/hail`

**One-liner (for the PR description, not the README line):** Phone, SMS & email — for agents.

**Category (target's internal taxonomy):** Communication

**Legend icons used:** 🐍 (Python codebase) ☁️ (Cloud Service) — per the target README's own `## Legend` key. No 🎖️ (this isn't an official implementation of a third-party product) and no OS icons (it's a server, not a local desktop tool).

**PR title:** `Add hail-hq/hail to Communication`

## Notes

- **Submission mechanism is Fork → branch → one-line README edit → PR**, per the target's own `CONTRIBUTING.md`. No web form, no account beyond GitHub, no separate asset upload — the whole submission _is_ the diff.
- **Agent fast-track:** `CONTRIBUTING.md` states PRs from automated agents get expedited review if the PR title ends in `🤖🤖🤖`. Use it if the actual PR author is an agent; drop it if a human is filing the PR by hand.
- **Alphabetization is manually maintained and drifts** — the live Communication section isn't perfectly sorted in a few places (contributors have appended near-neighbors without re-sorting). Re-verify the exact insertion point against the current `main` right before editing, don't rely solely on this file's snapshot.
- **SMS caveat:** SMS is a shipped, present-tense product capability of Hail (voice, SMS, email), but there is no `send_sms`/`list_sms` MCP tool yet — `mcp/hailhq/mcp/tools.py` exposes only call, email, and event/stats tools. The README line above says "Phone, SMS & email for AI agents" (true of the product) but scopes the _tool_ description to "call, email, and event tools" — don't let SMS drift into a claim that it's callable over MCP today.
- **Vendor accuracy:** voice is Twilio-backed, email is SES-backed (`core/hailhq/core/providers/voice/twilio.py`, `core/hailhq/core/providers/email/ses.py`). No other carrier/vendor is wired up — the README line above names no vendor at all, so this doesn't need a "coming soon" note here, but don't add one if asked to expand the copy.
- No published review-turnaround SLA; standard open-source PR review cadence applies. Track the PR number here once opened.
- **Submitted 2026-07-07:** PR opened at https://github.com/punkpeye/awesome-mcp-servers/pull/9561 (fork: `r13i/awesome-mcp-servers`, branch `add-hail-mcp-server`), title includes the 🤖🤖🤖 agent fast-track marker per this file's own guidance. Awaiting maintainer review.
- **Deprioritized 2026-07-07:** the repo's `glama-check` bot now requires a Glama score badge before merge (see PR #9561 comments). Glama listing/verification was abandoned (see `glama-mcp-registry.md`) — not pursuing the badge further. PR #9561 is left open as-is; may stall or get closed by the maintainer without the badge. Not actively worked further unless the bot requirement changes or the maintainer merges anyway.
- **Resumed 2026-07-07:** added the requested badge anyway (`commit 89abc62a`) — `[![hail MCP server](https://glama.ai/mcp/servers/hail-hq/hail/badges/score.svg)](https://glama.ai/mcp/servers/hail-hq/hail)`, placed right after the repo link per the file's actual established convention (100+ existing entries), not literally "after the description" per the bot's wording. The badge is a live dynamic image — it'll render whatever Glama's current state is (including "pending"/unscored) whenever viewed; not blocked on Glama's Build & Release check actually passing. Awaiting maintainer review again.
