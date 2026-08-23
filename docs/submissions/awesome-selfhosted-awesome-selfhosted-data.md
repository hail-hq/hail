---
target: "awesome-selfhosted (awesome-selfhosted-data)"
slug: awesome-selfhosted-awesome-selfhosted-data
category: github-list
url: "https://github.com/awesome-selfhosted/awesome-selfhosted-data"
score: 5.5
status: drafted
---

# awesome-selfhosted (awesome-selfhosted-data)

**Refreshed 2026-08-23 against current `CONTRIBUTING.md`, `addition.md`, `PULL_REQUEST_TEMPLATE.md`, `README.md` Milestones, and `hail-website/lib/home-copy.ts`.**

## TODO

- [ ] **Blocker: first release is not yet 4 months old.** Earliest GitHub Release is `v0.1.0`, published 2026-05-01 (`gh release list`). Earliest safe submit date: **2026-09-01**. Maintainers close early PRs with a canned reply.
- [ ] **Risk: "Software that depends on a specific cloud provider" is in "What does not qualify".** Voice needs LiveKit Cloud (`docs/public/self-host/livekit-cloud.md`: self-hosted SFU is a later milestone). SMS and email run without it. Mitigation: `depends_3rdparty: true` + description leads with the channels. Decide: submit on 2026-09-01 anyway, or wait for the self-hosted SFU milestone.
- [ ] **Ban risk: "Machine/LLM-generated contributions that do not respect project guidelines… will result in a ban."** Submit from `r13i`'s own account. PR = one file + checked template boxes. No extra prose.
- [ ] Run `make awesome_lint` locally on `software/hail.yml` before pushing (Steps, step 4).
- [ ] Re-verify the day you submit: no `software/hail*.yml` exists (checked 2026-08-23: none), no open/closed PR or issue mentions Hail (checked 2026-08-23: none).
- [ ] Submit PR.
- [ ] On merge: set `status: confirmed`, paste the `awesome-selfhosted.net` entry URL in Notes.

## Steps to submit

1. Wait until **2026-09-01 or later**.
2. Fork and clone:
   ```bash
   gh repo fork awesome-selfhosted/awesome-selfhosted-data --clone
   cd awesome-selfhosted-data
   git checkout -b add-hail
   ```
3. Create `software/hail.yml` with the exact YAML from **Content**.
4. Lint:
   ```bash
   make install
   make awesome_lint
   ```
   Fix anything it reports, re-run until clean.
5. Commit and push:
   ```bash
   git add software/hail.yml
   git commit -m "add Hail"
   git push -u origin add-hail
   ```
6. Open the PR: `gh pr create --base master --title "add Hail" --body-file -` and paste the **PR body** from Content. Or open it in the GitHub web UI; the PR template auto-fills — check every box.
7. Respond to review in the same PR. Merge happens ~1 week after approval.
8. Confirm live: search "Hail" at https://awesome-selfhosted.net.

## Content

File: `software/hail.yml`

```yaml
name: Hail
website_url: https://hail.so
source_code_url: https://github.com/hail-hq/hail
description: Phone calls, SMS, and email for AI agents, consumed via MCP server, REST API, CLI, or Python SDK.
licenses:
  - AGPL-3.0
platforms:
  - Python
  - Docker
tags:
  - Communication - Custom Communication Systems
  - Generative Artificial Intelligence (GenAI)
depends_3rdparty: true
```

PR title: `add Hail`

PR body (the repo's template; every box checked):

```markdown
Thanks for taking the time to suggest an addition to awesome-selfhosted!

To ensure your Pull Request is dealt with swiftly, please check the following (check the boxes `[x]`):

- [x] Submit one item per pull request. This eases reviewing and speeds up inclusion.
- [x] You have searched the repository for any relevant [issues](https://github.com/awesome-selfhosted/awesome-selfhosted-data/issues) or [PRs](https://github.com/awesome-selfhosted/awesome-selfhosted-data/pulls), including closed ones.
- [x] Any software you are adding is not already listed at any of [awesome-sysadmin](https://github.com/awesome-foss/awesome-sysadmin), [staticgen.com](https://www.staticgen.com/), [staticsitegenerators.bevry.me](https://staticsitegenerators.bevry.me/), [dbdb.io](https://dbdb.io/browse).
- [x] The file you are adding is formatted as described in [addition.md](https://github.com/awesome-selfhosted/awesome-selfhosted-data/blob/master/.github/ISSUE_TEMPLATE/addition.md).
- [x] `Demo` links should only be used for interactive demos, i.e. not video demonstrations. If login credentials are required to access the demo, please link to the credentials directly.
- [x] Comments and unused optional fields have been removed.
- [x] The file you are adding uses [kebab-case](https://en.wikipedia.org/wiki/Letter_case#Kebab_case) file naming, for example `my-awesome-software.yml`.
- [x] Values for `platform` should match the platforms required to install and run the software.
- [x] Any software project you are adding to the list is actively maintained.
- [x] Any software project you are adding was first released more than 4 months ago.
- [x] Any software project you are adding has working installation instructions.
- [x] You understand that your Pull Request will be merged at least ~1 week after approval, depending on maintainers time.
```

Field notes:

- `description`: 97 chars (limit 250). Sentence case. No "self-hosted", "open-source", "free" (redundant per guidelines). No leading "A …". Channel claims match `README.md` Milestones (2026-08-23): outbound calls, SMS in/out, email in/out — all checked. Matches site copy (`home-copy.ts`: "Give your AI agent a voice, a phone number, and an inbox… Send SMS and agent mail"). Leads with "Phone calls, SMS, and email" so it reads as a running service, not an SDK (SDKs are in "What does not qualify").
- No `(alternative to …)` suffix. No single well-known product is a clean match; adding one invites review debate.
- `licenses`: `AGPL-3.0` is the identifier in `licenses.yml` (line 10, checked 2026-08-23).
- `platforms`: only what runs the server — Python (api, voicebot, mcp) and Docker (`docker compose up`). Go is dropped: the CLI is a client, and the PR template says platforms "match the platforms required to install and run the software".
- `tags`: first tag is the only one shown in single-page mode. `Communication - Custom Communication Systems` is where Chatwoot lives. `Generative Artificial Intelligence (GenAI)` second. `Communication - SIP` skipped (PBX software).
- `depends_3rdparty: true`: Twilio (calls, SMS), LiveKit Cloud (media), AWS SES (email), Deepgram/Speechmatics (STT), Cartesia/ElevenLabs (TTS), one LLM vendor or BYO endpoint.
- No `demo_url`: no interactive demo. No `related_software_url`: no plugin ecosystem.
- Do not add `stargazers_count`, `updated_at`, `commit_history`, `current_release` — a bot fills those after merge.

## Notes

- 4-month rule date math: `v0.1.0` published 2026-05-01T05:58:35Z → 4 months = 2026-09-01. `sdk-v0.0.1` (tag 2026-04-24) and `cli-v0.1.0` (tag 2026-04-29) have no GitHub Release; do not cite them.
- Inbound calls are unchecked in Milestones. "Phone calls" without "inbound" is accurate. Do not reword to "answer calls".
- Not-yet-shipped items that do not affect the YAML: inbound calls, Telnyx, Whisper/AssemblyAI STT, Deepgram Aura TTS, recording, self-hosted LiveKit SFU.
- Review turnaround: days to ~2 weeks; merge ≥ ~1 week after approval.
- Contact used: none yet. Public GitHub PR from `r13i`.
