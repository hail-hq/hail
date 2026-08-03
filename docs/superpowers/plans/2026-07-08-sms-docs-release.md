# SMS Docs & Release Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the loop on the SMS rollout: document setup for self-hosters, fix a real env-var-documentation gap left by the earlier phases, update the changelog/README/CLI+SDK release notes, and verify (not duplicate) the legal-doc work a parallel workstream has already substantially completed.

> **⚠️ PLAN STATUS (audited 2026-07-09 against `main`).** The **outbound-only** `0.10.0` release already shipped this week (see commits `a79e93e`, `d51cd15`, and PR #9 `release/0.10.0`). As a result most of Tasks 1, 4, and 5 are **already DONE** — verify, do not redo. Only two deliverables are genuinely outstanding: **Task 2 (`docs/setup/sms.md` — does not exist yet)** and **Task 3 (`docs/operations.md` 10DLC note — not present yet)**.
>
> **IMPORTANT scope correction:** the plan's Global Constraints assume Inbound & Compliance, Numbers & Sender ID, and Console UI phases are all merged. **They are NOT.** Only Outbound Core shipped. CHANGELOG's `## [0.10.0]` "Deferred to next milestone" section confirms inbound SMS, self-serve number provisioning, per-org Sender ID, and the Console UI are still pending. Therefore: the **abuse-monitor `.env.example` work in Task 1 must NOT be done** — those `Settings` fields don't exist in `core/hailhq/core/config.py` yet (verified: no `hail_sms_abuse_*` / `hail_abuse_monitor_*` fields, no abuse-monitor code in `api/`/`mcp/`/`core/`), so documenting them would violate the "`.env.example` must document the true default" rule. They belong to the (unmerged) Inbound & Compliance plan. Likewise the `docs/setup/sms.md` draft's Inbound/Sender-ID sections describe unshipped endpoints — see per-task notes.

**Architecture:** Almost entirely prose/config, not application code — this is the lightest of the four SMS phase plans. The one piece of real bookkeeping is auditing every new `Settings` field the prior three phases introduced and confirming each is documented in `.env.example`, per this repo's own CLAUDE.md invariant ("Adding a new env var? Update `.env.example` in the same commit") — a check that was missed for at least the SMS velocity-cap settings, confirmed by direct read.

**Tech Stack:** Markdown, `.env.example`, `CHANGELOG.md`/`README.md` conventions already established in this repo.

## Global Constraints

- ⚠️ **OUTDATED ASSUMPTION (corrected 2026-07-09):** This bullet assumed all four SMS phases were merged. In reality **only Outbound Core shipped** (`0.10.0`). Inbound & Compliance, Numbers & Sender ID, and Console UI are still deferred (per `CHANGELOG.md` "Deferred to next milestone"). Release notes must describe **outbound only** — which the shipped `[0.10.0]` entry already does correctly.
- **Legal docs (`hail-website/content/legal/{aup,terms,dpa}.md`, `hail/docs/legal/{ropa,dpia-summary}.md`) are already substantially updated by a parallel workstream** — confirmed by direct read during this plan's research: SMS is already described as live (not "planned") in the public legal docs, and a full RoPA entry + DPIA sequencing note for SMS already exist. This plan's only legal-doc task is a final verification pass once the code actually ships, per the DPIA's own explicit note that it's "a hard gate before the SMS channel actually goes live in production, not a formality" — not re-authoring anything already done.
- **`.env.example` gap** — ⚠️ **PARTIALLY RESOLVED, verify:** `HAIL_VELOCITY_SMS_PER_HOUR=100`/`HAIL_VELOCITY_SMS_PER_DAY=1000` are **already present** (`.env.example:177-178`) and backed by real `Settings` fields (`core/hailhq/core/config.py:215-216`). ✅ Done. The abuse-monitoring settings (`HAIL_SMS_ABUSE_*`, `HAIL_ABUSE_MONITOR_POLL_SECONDS`) are **NOT** missing-and-to-be-added here — those `Settings` fields do not exist yet (Inbound & Compliance phase is unmerged), so they must NOT be added to `.env.example` until that phase ships. See the PLAN STATUS box above.

---

## File Structure

```
docs/setup/sms.md                    # new       — 🟢 TO-DO (absent; outbound-only content)
docs/operations.md                   # modified  — 🟢 TO-DO (10DLC note not yet present)
.env.example                         # modified  — ✅ DONE (velocity vars present; abuse vars out of scope)
CHANGELOG.md                         # modified  — ✅ [0.10.0] entry done; only narrow the stale "Deferred to v1.x" line
README.md                            # unchanged — ✅ SMS Outbound already ticked; leave Inbound unchecked
```

---

### Task 1: `.env.example` audit and fix — ✅ MOSTLY DONE (verify only)

> **Audited 2026-07-09:** The SMS velocity vars are already in `.env.example` (lines 177-178) and match their `config.py` defaults. **No edit is needed** for the velocity vars. Do **NOT** add the abuse-monitor vars — that feature is unshipped (Inbound & Compliance phase, not merged). This task collapses to a one-line verification; there is nothing to commit unless the velocity lines are somehow absent at execution time.

**Files:**

- Modify: `.env.example` (⚠️ likely no change needed — verify)

- [ ] **Step 1: Confirm the current gap** — ✅ Already verified: velocity vars present, abuse vars intentionally out of scope.

Run: `grep -n "TWILIO\|VELOCITY_SMS\|ABUSE" .env.example` and cross-reference against every `Settings` field added across the three prior SMS phases: `twilio_account_sid`/`twilio_auth_token` (already present, pre-dates SMS), `hail_velocity_sms_per_hour`/`_per_day` (Outbound Core), `hail_sms_abuse_window_hours`/`_min_sends`/`_max_opt_out_rate`, `hail_abuse_monitor_poll_seconds` (Inbound & Compliance). Confirm which are actually missing before editing (this plan's research found the SMS velocity and abuse settings missing; re-verify against the checkout's actual current state at implementation time, since the exact set of new settings depends on what the prior phases actually shipped).

- [ ] **Step 2: Add the missing entries** — ✅ **DONE for the velocity vars; abuse vars OUT OF SCOPE.**

The two velocity vars are already present at `.env.example:177-178`:

```
HAIL_VELOCITY_SMS_PER_HOUR=100
HAIL_VELOCITY_SMS_PER_DAY=1000
```

⚠️ **Do NOT add the abuse-monitor block below** — kept here only to record the original intent. These `Settings` fields do not exist in `config.py` yet (Inbound & Compliance phase unmerged); adding them now would document env vars the app doesn't read. They belong to the inbound-compliance plan, not this one.

```
# DEFERRED — do not add until the Inbound & Compliance phase ships:
# HAIL_SMS_ABUSE_WINDOW_HOURS=24
# HAIL_SMS_ABUSE_MIN_SENDS=20
# HAIL_SMS_ABUSE_MAX_OPT_OUT_RATE=0.05
# HAIL_ABUSE_MONITOR_POLL_SECONDS=3600
```

- [ ] **Step 3: Verify the app still boots with a fresh `.env` from the template**

Run: `cp .env.example .env.local.test && diff <(grep -oE '^[A-Z_]+=' .env.example) <(grep -oE '^[A-Z_]+=' .env.local.test)` (sanity check the copy is faithful), then confirm no new required (non-defaulted) settings were missed by checking `core/hailhq/core/config.py` for any field without a default that also isn't in `.env.example` — `twilio_account_sid`/`twilio_auth_token` already default to `""` so the app boots either way; nothing new should be a hard-required field with no default.

- [ ] **Step 4: Commit** — ⚠️ **Skip if no change.** The velocity vars are already committed (they shipped with `0.10.0`). Only commit if the verification in Steps 1-3 surfaces a genuine, in-scope gap. Do not manufacture a diff.

```bash
git add .env.example
git commit -m "docs: document missing sms velocity env vars"
```

---

### Task 2: `docs/setup/sms.md` — 🟢 GENUINELY TO-DO (file does not exist)

> **Audited 2026-07-09:** `docs/setup/sms.md` is absent (`ls docs/setup/` has no `sms.md`). This task stands.
>
> ⚠️ **Scope caveat:** only **Outbound Core** has shipped in `0.10.0`. The draft below has sections that describe **unshipped** endpoints — trim or clearly mark them as forthcoming so the doc doesn't claim features that don't exist yet:
>
> - **§2 Sender ID** (`PATCH /sms/sender-id`) — not shipped (custom per-org Sender ID is in the CHANGELOG's "Deferred to next milestone").
> - **§3 Inbound** (`/sms/inbound` webhook, STOP/HELP/START) — not shipped (inbound SMS is deferred).
> - **§1 step 4** (`POST /numbers/{id}/enable-sms`) and self-serve `POST /numbers` — not shipped (self-serve provisioning is deferred; numbers are still seeded via SQL per `docs/operations.md`).
>
> Write the doc for what actually ships today (10DLC registration guidance + the velocity/env-var pointer), and defer the rest. Verify each referenced endpoint exists in `openapi/openapi.yaml` before documenting it.

**Files:**

- Create: `docs/setup/sms.md`

- [ ] **Step 1: Write the doc**

Follow `docs/setup/twilio.md`'s exact brevity convention (short numbered sections, each answerable in one glance — per the "brief docs" tenet, one screen):

```markdown
# SMS (Twilio)

Builds on the [Twilio setup](./twilio.md) — same account, same credentials.

## 1. A2P 10DLC registration (one-time, operator task)

US SMS requires a Twilio A2P 10DLC Brand + Campaign registered once for
the whole platform (not per-org — see the design spec's accepted
shared-campaign tradeoff). From [console.twilio.com](https://console.twilio.com):

1. **Trust Hub → Customer Profiles** — confirm or create your business
   profile (KYB). This is usually already done if you're sending voice
   calls through this same account.
2. **Messaging → Regulatory Compliance → A2P 10DLC → Register Brand.**
   Budget 1-2 weeks for standard-tier vetting.
3. **A2P 10DLC → Register Campaign** under that brand. Budget an
   additional 10-15 days for Twilio's campaign review.
4. Numbers get attached to a Messaging Service per-org automatically the
   first time an org enables SMS (`POST /numbers/{id}/enable-sms`) — no
   manual per-org Twilio Console step.

Non-US numbers (most of the rest of the world) aren't gated by this —
they can send as soon as a dedicated number is acquired.

## 2. Sender ID

Orgs can set a custom alphanumeric Sender ID (`PATCH /sms/sender-id`) for
outbound-only sends to countries that don't require pre-registration
(Germany, UK). US, Canada, and registration-required countries (Australia)
always use the org's dedicated number or the platform default (`"HAIL"`)
instead — see the design spec's Sender ID decisions for the full
per-country breakdown.

## 3. Inbound

Configure your Twilio Messaging Service's inbound webhook URL to
`https://<your-api-host>/sms/inbound`. Signature-verified against your
`TWILIO_AUTH_TOKEN` — no separate secret to configure.

## 4. Env vars

See `.env.example`'s SMS section for velocity-cap and abuse-monitoring
defaults — tune `HAIL_SMS_ABUSE_MAX_OPT_OUT_RATE` etc. once you have real
traffic data; the shipped defaults are a conservative starting guess.
```

- [ ] **Step 2: Cross-check against docs/contributing.md's doc-writing conventions**

Confirm the new file follows the "agent-first docs" tenet (concrete runnable example, links to canonical sources rather than paraphrasing) — the draft above links to `twilio.md` and the design spec rather than re-explaining Twilio Console navigation in exhaustive detail, consistent with that tenet.

- [ ] **Step 3: Commit**

```bash
git add docs/setup/sms.md
git commit -m "docs: add SMS setup guide"
```

---

### Task 3: `docs/operations.md` note — 🟢 GENUINELY TO-DO

> **Audited 2026-07-09:** `docs/operations.md` has no SMS/10DLC note and no `0.10.0` entry in its release material (grep for `sms`/`10dlc`/`0.10.0` finds only the two SQL number-seeding snippets at lines 133 and 159, plus a "SMS, Email channels" future mention at line 697). This task stands.
>
> ⚠️ Adjust the note below: self-serve `POST /numbers` and `POST /numbers/{id}/enable-sms` are **not shipped** (deferred). Numbers are still seeded manually via the SQL at `operations.md:133`/`:159`. Frame the SMS note around the manual-seeding reality plus the one-time 10DLC registration, not the unshipped self-serve flow.

**Files:**

- Modify: `docs/operations.md`

- [ ] **Step 1: Add the note**

Find the existing note about manually seeding a `phone_numbers` row via SQL (referenced in prior research at `docs/operations.md:133,159`) and add, immediately after it:

```markdown
**SMS**: the platform-level A2P 10DLC brand/campaign registration is a
one-time operator task — see [docs/setup/sms.md](setup/sms.md). Once an
org has a dedicated number (self-serve via `POST /numbers`, superseding
the manual seeding above for orgs that need SMS), enabling SMS on it
(`POST /numbers/{id}/enable-sms`) is fully self-serve — no further
operator action per org.
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations.md
git commit -m "docs: note the one-time sms 10dlc registration step in operations"
```

---

### Task 4: `CHANGELOG.md` and `README.md` — ✅ MOSTLY DONE (verify + one small fix)

> **Audited 2026-07-09:**
>
> - **CHANGELOG `## [0.10.0]` entry: ✅ already written** (`CHANGELOG.md:5-51`, dated `2026-07-09`, with component versions `sdk-v0.7.0` / `cli-v0.10.0`, an SMS-outbound section, and a "Deferred to next milestone" list). Do **not** re-author it.
> - **Version bumps: ✅ done** — `sdk/pyproject.toml` `version = "0.7.0"` and `sdk/hail/__init__.py` `__version__ = "0.7.0"`; CLI cut as `cli-v0.10.0`.
> - **README SMS Outbound checkbox: ✅ already ticked** (`README.md:100` `- [x] Twilio`).
> - **One genuine remaining nit (Step 1):** the OLD "Deferred to v1.x" section still lists `- SMS channel (Twilio outbound and inbound).` at `CHANGELOG.md:287`. Since only outbound shipped, update that line to reflect that **outbound is done, inbound is still deferred** (e.g. narrow it to `- SMS channel — inbound (Twilio).`), rather than deleting it outright. This is the only CHANGELOG edit left for this outbound release.

**Files:**

- Modify: `CHANGELOG.md` (only the stale "Deferred to v1.x" line; the `[0.10.0]` entry is done)
- Modify: `README.md` (⚠️ likely no change — see Step 2)

- [ ] **Step 1: ✅ `[0.10.0]` entry already exists — only fix the stale "Deferred to v1.x" line**

The `## [0.10.0]` section is already present and correct (`CHANGELOG.md:5-51`). **Do not add another one.** The only remaining edit: at `CHANGELOG.md:287` the "Deferred to v1.x" section still reads `- SMS channel (Twilio outbound and inbound).` — narrow it to inbound-only since outbound shipped. The original "add a new section" instructions below are retained for historical reference only:

```markdown
## [0.10.0] — <implementation date>

Component versions cut alongside this release: **`sdk-vX.Y.0`**, **`cli-vX.Y.0`**.

- SMS is now a first-class channel: `POST/GET /sms`, inbound via Twilio
  webhook, opt-out (STOP/HELP/START) handling, a `hail sms` CLI command,
  `Client.sms` in the Python SDK, and `send_sms`/`get_sms`/`list_sms` MCP
  tools. Requires a dedicated phone number (self-serve via `POST /numbers`)
  — the shared voice pool doesn't support SMS.
- Custom alphanumeric Sender ID (`PATCH /sms/sender-id`) for outbound-only
  sends to no-pre-registration countries (Germany, UK); US/Canada and
  registration-required countries always use a dedicated number or the
  platform default.
- New generic, cross-channel number-provisioning API (`POST/GET /numbers`)
  — not SMS-specific; voice numbers can eventually flow through the same
  surface.
```

Adjust the exact version numbers to whatever the actual next SDK/CLI releases are cut as (check the most recent `chore(release): ...` commit for the current versions before incrementing).

- [ ] **Step 2: Check off `README.md`'s SMS milestones** — ✅ **Outbound already ticked; leave Inbound unchecked.**

⚠️ **Correction:** the original plan ticked BOTH Outbound and Inbound, but inbound SMS is **not shipped**. Current README state (`README.md:97-102`) is already correct and needs **no change**:

```markdown
### SMS

- Outbound
  - [x] Twilio ← already ticked (shipped in 0.10.0)
- Inbound
  - [ ] Twilio ← correct: inbound is deferred, keep unchecked
```

Do not tick Inbound.

- [ ] **Step 3: Commit** — ⚠️ Only `CHANGELOG.md` should have a diff (the narrowed "Deferred to v1.x" line). `README.md` needs no change. Skip the commit if nothing changed.

```bash
git add CHANGELOG.md
git commit -m "docs(changelog): narrow deferred sms entry to inbound-only"
```

---

### Task 5: Legal-doc verification pass — ✅ SMS-content clauses DONE (verify only)

> **Audited 2026-07-09:** The parallel legal workstream's SMS updates are **already landed**. Verified present:
>
> - `hail-website/content/legal/privacy.md:166`, `terms.md:154-157`, `dpa.md:41,85,112,132` — all now cover **"SMS/text message content"** in the retention / encryption-at-rest / DSAR clauses alongside call transcripts and email.
> - `aup.md:47` describes **"account velocity caps"** as a live control (present-tense).
> - `hail/docs/legal/ropa.md:68` has the full **"2a. Activity: Outbound SMS/text messaging"** entry; `dpia-summary.md:5` names SMS as a pre-launch review gate.
> - **No stale "planned/future" language** remains near SMS/velocity/suppression in the four website legal docs (grep found none).
>
> So Steps 1 and 3 are effectively **satisfied — verify, do not re-author.** Step 2 (the human DPIA sign-off gate) remains a genuine non-code launch check to surface.

**Files:**

- None modified unless a genuine gap is found — this task is a verification checklist, not new authoring, and the SMS-content + stale-language work is already done (see audit box above).

- [ ] **Step 1: Confirm current state** — ✅ Already verified done (see audit box). Re-run the greps only to reconfirm; expect no edits.

Run, from the `hail` repo root: `grep -n -i "sms" docs/legal/ropa.md docs/legal/dpia-summary.md` and, from the `hail-website` repo root: `grep -n -i "sms" content/legal/aup.md content/legal/terms.md content/legal/dpa.md`. As of this plan's research, all of these already describe SMS in present-tense/live language (not "planned/future") and the RoPA already has a full "2a. Activity: Outbound SMS/text messaging" entry with an explicit sequencing note flagging the DPIA as a pre-launch gate.

- [ ] **Step 2: Confirm the DPIA gate is actually satisfied, not just documented**

`docs/legal/dpia-summary.md` names "review before any new communication channel (including SMS/text messaging)" as a requirement. This is a real founder/lawyer sign-off step, not something an implementation task can complete on its own — confirm with the user/founder that this review has actually happened before treating the SMS channel as cleared for production traffic. If it hasn't, this is a launch blocker independent of code-readiness, and should be surfaced explicitly rather than silently assumed complete because the doc exists.

- [ ] **Step 3: If any doc still says "planned" or "future" for SMS** — ✅ Verified 2026-07-09: no stale "planned"/"future" SMS language remains in the four website legal docs. Nothing to flip. Re-grep to reconfirm only.

- [ ] **Step 4: No commit needed if nothing changed**

If Step 1-3 confirm everything is already correct, this task closes with no diff. Do not manufacture a change to have something to commit.

---

## Self-Review Notes

- **Spec coverage**: covers the design spec's "Docs, changelog, release notes" section in full, plus a genuine gap found by direct verification (`.env.example`) that the section didn't originally call out.
- **Placeholder scan**: `<implementation date>` and version numbers (`vX.Y.0`) in the CHANGELOG task are explicitly marked as "fill in from real state at implementation time," not silently guessed — matches this repo's own convention of checking the most recent release commit before incrementing, not a vague placeholder left unresolved.
- **Scope discipline**: Task 5 is deliberately a verification checklist, not new authoring — duplicating the parallel workstream's already-substantial legal-doc work would be wasted effort and a likely source of merge conflicts.

## This completes the SMS feature's planned phases

Prior plans: SMS Outbound Core, SMS Inbound & Compliance, SMS Numbers & Sender ID, SMS Console UI & Billing. This plan closes out documentation and release bookkeeping. Any further SMS work (MMS, per-org 10DLC registration, international dedicated numbers, generalizing abuse monitoring to voice/email) is explicit future work per the design spec, not part of this plan sequence.
