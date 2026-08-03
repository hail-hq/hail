# Registry & directory submissions for Hail — design

## Goal

Get Hail (the platform) and Hail MCP (the remote MCP server) discovered by the people most likely to use them — AI agent builders — by submitting to the highest-impact registries, directories, awesome-lists, and relevant subreddits. Output is a set of ready-to-submit drafts; no live submission happens without a human clicking submit.

## Non-goals

- No autonomous execution of external, hard-to-reverse actions: no auto-opening PRs against third-party repos, no auto-posting to Reddit/Product Hunt/HN, no auto-filling web forms. Every registry that requires an account, a form, or a public PR gets a **drafted file**, not a live action.

## Feature-claim policy (explicit call, overrides default "don't overclaim" instinct)

Core capabilities — voice calls, SMS, email, analytics, deliverability checks, etc. — are written up **as shipped, present-tense, working features**, regardless of today's milestone checkbox state in `README.md`. This is a deliberate positioning choice: these are expected to land imminently and the team owns the gap risk, not the submission copy.

**Provider/vendor breadth is the one thing that stays honest to current state.** Which specific carrier/vendor backs a capability (e.g. Twilio-only today vs. Twilio+Telnyx, Deepgram-only STT vs. +Whisper/+AssemblyAI) is listed accurately — multi-provider redundancy that isn't shipped yet goes on the roadmap/"coming soon" list in a submission, never implied as already live. So: "Hail sends SMS" — yes, write it as true. "Hail sends SMS over Twilio or Telnyx" — only if Telnyx is actually wired up; otherwise Telnyx is a roadmap bullet.

_Risk flag, noted once and not revisited unless asked: some registries (Product Hunt, the MCP registry, moderated subreddits) may check claims against a live demo or the repo itself — if a reviewer tries SMS/analytics before it's live, the gap is visible. Proceeding per the instruction above; the team accepts that risk._

## Sourcing rule (applies to every draft)

- **Voice and visual identity** come from the Claude Design project "Hail.so Visual Identity" (`e175bb75-8f2d-48c6-9106-05354e94fdc1`): tone is direct / operational / wry / technical (`identity.html` §05 Voice); logo/wordmark usage rules from `logos/README.md`; concrete assets already mirrored into `hail-website/public/assets/`.
- **Facts other than core-capability claims** (install steps, URLs, license, actual tool names, which specific providers are wired up) come from the live repo, verified against **code**, not just prose, because both drift:
  - Wired-up providers: `core/hailhq/core/providers/<channel>/`
  - Live MCP tool list: `mcp/hailhq/mcp/tools.py`
  - What's actually published externally: `pyproject.toml` (SDK), `cli/` release process, npm scope `@hail-hq/`
  - Recent changes / in-flight work: `CHANGELOG.md` and `docs/superpowers/specs/*` (useful for spotting which provider-breadth claims are still roadmap, e.g. `2026-07-06-sms-support-design.md`)
- **`launch/show-hn.html`** in the Claude Design project is tone/pacing reference only — it describes a stale local-`npx`/`@hail/mcp` install flow and the wrong GitHub org (`hail-so` vs. actual `hail-hq`), both superseded by the current remote-only MCP decision in `docs/setup/mcp.md`. Never sourced for facts.
- Applies the feature-claim policy above: fact-check rewrites provider-breadth overclaims (e.g. "Twilio or Telnyx" when only Twilio is wired) but leaves core-capability present-tense claims (SMS, analytics, deliverability) alone.

## Architecture: two-phase Workflow, approval gate between phases

Phase 1 is a research-only `Workflow` run. Its output (a ranked shortlist) is reviewed and trimmed by the user before Phase 2 spends any effort drafting. This avoids drafting for targets that get rejected.

### Phase 1 — Research Workflow

Parallel researcher agents fan out by category. Each agent fetches the **live** submission requirements for its candidates (not from training data) and returns structured results: name, URL, submission mechanism (PR / CLI tool / web form / community post), requirements, eligibility fit, and an estimated-reach note.

Categories:

1. **MCP-specific registries**: official MCP registry (modelcontextprotocol/registry), mcp.so, Smithery, Glama, PulseMCP, OpenTools, Cursor directory, Claude connector directory.
2. **Curated GitHub lists**: awesome-mcp-servers, awesome-selfhosted (Hail is self-hostable, AGPLv3).
3. **General AI-tool directories**: There's An AI For That, Futurepedia, Toolify, and similar.
4. **Dev/startup directories**: Product Hunt, Hacker News Show HN, AlternativeTo.
5. **Reddit communities**: search for subreddits relevant to AI agents / dev tools / self-hosting / voice AI / MCP (e.g. r/LocalLLaMA, r/ClaudeAI, r/mcp, r/selfhosted, r/SideProject, r/opensource). For each, read the sidebar/wiki rules to confirm self-promotion is allowed or there's a standing showcase thread, and record the compliant post angle (a "here's a cool use case I built" showcase, not an announcement) plus the rule/thread it satisfies.

A synthesis agent scores every candidate on **reach × ICP fit × submission effort** (subreddits also weighted for removal/ban risk if done wrong) and returns a ranked list, highest impact first, with a natural cut line rather than a fixed count.

**Checkpoint**: the ranked list is presented to the user, who approves/trims it. Phase 2 only runs against approved entries.

### Phase 2 — Drafting Workflow

Pipeline, one item per approved target, three stages:

1. **Draft** — submission content in Hail's brand voice, built from that target's actual required fields (one-liner, long description, tags/category, install/usage snippet, asset references into `hail-website/public/assets`). Special cases:
   - **Official MCP registry**: include the actual `server.json` manifest content, plus the DNS/GitHub ownership-verification and `mcp-publisher` CLI steps the user runs themselves (we don't hold their DNS/GitHub credentials).
   - **Subreddits**: a showcase-post draft framed as "here's what I built," matched to the specific rule/thread identified in research, not a listing/announcement.
2. **Fact-check** — adversarial pass checking every claim against the code sources listed under Sourcing rule above, applying the feature-claim policy: core-capability claims (SMS, analytics, deliverability, etc.) are left as present-tense/shipped; only provider-breadth overclaims (e.g. implying Telnyx when only Twilio is wired) and non-feature facts (URLs, install steps, license, tool names) get rewritten or flagged.
3. **Write** — lands as `docs/submissions/<slug>.md`.

### Output artifacts (in the `hail` repo)

- `docs/submissions/<slug>.md` — one self-contained file per target. Each file includes:
  - A **TODO checklist** at the top (account needed? assets ready? draft reviewed? submitted? — checkable items).
  - **Structured, numbered steps to execute** the submission end-to-end (where to go, what to paste, what to click, what to wait on).
  - The actual copy-paste-ready content (description fields, tags, manifest, post text).
- `docs/submissions/README.md` — index of all targets, **sorted by relevance/impact score, highest first**, each row linking to its file and showing status (drafted / submitted / rejected / n/a).

Nothing is posted, filed, or opened as a PR automatically. Every deliverable is a reviewable file with a checklist the user works through by hand.

## Handoff safety

Assume this work may need to continue under a lower-capacity model, or a human with no prior context, at any point — including mid-phase. Every artifact this design produces is built so continuation never depends on re-deriving intent:

- **State lives in files, not memory.** `docs/submissions/README.md` is the single source of truth for what's done — status per target (drafted / submitted / rejected / n/a), updated as work happens, never implied only by conversation history.
- **Steps are imperative and concrete, never conceptual.** Each `docs/submissions/<slug>.md` reads as a numbered checklist a non-expert could execute literally — "go to X, paste Y, click Z" — not "adapt the pitch for this audience." Judgment calls (positioning, tone, ranking) are resolved during drafting, not deferred into the instructions.
- **The two-phase Workflow structure is itself resumable.** Both the research and drafting `Workflow` runs support `resumeFromRunId` — if a run is interrupted, re-invoking with the same script and args replays cached agent results and only continues the unfinished tail. This means an interruption mid-Phase-2 costs at most the in-flight item, not a restart from zero.
- **The ranked shortlist and its scoring rationale are written down**, not just presented in chat — so approval/trim decisions survive a session boundary and don't need to be re-explained to whoever (or whatever) picks this up next.
