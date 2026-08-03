---
target: "awesome-selfhosted (awesome-selfhosted-data)"
slug: awesome-selfhosted-awesome-selfhosted-data
category: github-list
url: "https://github.com/awesome-selfhosted/awesome-selfhosted-data"
score: 5.5
status: drafted
---

# awesome-selfhosted (awesome-selfhosted-data)

## TODO

- [ ] **Blocker: first tagged release is not yet 4 months old.** The earliest actual GitHub Release is `Hail v0.1.0`, published 2026-05-01T05:58:35Z (verified via `gh release list`, not just `git tag`). Today is 2026-07-07 (~2.2 months old). The list's stated rule is "first released more than 4 months ago" — do not open the PR/issue before **2026-09-01**. (`sdk-v0.0.1` and `cli-v0.1.0` are earlier git tags but have no corresponding GitHub Release, so they likely don't count toward this rule — the safe reference point is `v0.1.0`.)
- [ ] No account needed to open an issue; a free GitHub account is needed to submit a PR (fork-based).
- [ ] Confirm `https://github.com/hail-hq/hail` is public and `v0.1.0` stays a real GitHub Release (not just a CHANGELOG entry) at submission time.
- [ ] Confirm `hail.so` resolves and is the canonical marketing site (used as `website_url`).
- [ ] Decide/confirm `depends_3rdparty: true` is acceptable messaging — Hail requires Twilio, LiveKit Cloud, AWS SES, Deepgram, Cartesia/ElevenLabs, and one LLM vendor even when self-hosted.
- [ ] **Do not describe SMS as shipped in this submission.** Verified against `README.md` Milestones and `core/hailhq/core/providers/` (only `voice/twilio.py` and `email/ses.py` exist — no `sms/` module at all): SMS outbound and inbound are both unchecked/unshipped project-wide, not just missing a specific vendor. The drafted description below only claims voice + email, which are real, wired capabilities today.
- [ ] Locally run `make awesome_lint` against `software/hail.yml` before opening the PR (see Steps).
- [ ] Open PR or issue.
- [ ] Address any maintainer review comments (they check licenses.yml/tags/platforms slugs and the 4-month rule by hand).
- [ ] Confirm merged and live on awesome-selfhosted.net.

## Steps to submit

1. Wait until **2026-09-01 or later** (4 months after the `v0.1.0` GitHub Release date) — the maintainers close submissions that fail this rule on sight.
2. Go to https://github.com/awesome-selfhosted/awesome-selfhosted-data and click **Fork** (top right) to fork it to your own account. (Skip this and steps 3–6 if you'd rather file an issue — see step 7.)
3. In your fork, create a new file at path `software/hail.yml` (use the web UI "Add file → Create new file", or clone locally and add it there).
4. Paste the full YAML block from the **Content** section below into `software/hail.yml`, exactly as written (no comments, no unused optional fields).
5. Commit directly to a new branch (the GitHub web UI prompts for this when you click "Propose changes"), e.g. branch name `add-hail`.
6. Click **Compare & pull request**. Title the PR `Add Hail`. Leave the PR body empty or add one line: "New self-hosted software addition: Hail." Submit the PR against `awesome-selfhosted/awesome-selfhosted-data:master`.
7. **Alternative if you don't want to use PRs:** open a new issue at https://github.com/awesome-selfhosted/awesome-selfhosted-data/issues/new/choose, pick the "New software addition" template, and paste the same YAML block from Content into the template's code block.
8. Before submitting (PR route), verify locally: clone your fork, run `make awesome_lint` (see the repo's `Makefile` / `make help` for the exact target — it validates the YAML schema, license/tag/platform slugs, and description formatting). Fix any lint failures and re-run before pushing.
9. Wait for CI (the repo runs the same lint in GitHub Actions on every PR) and for a maintainer review. Respond to any requested changes in the same PR.
10. Once merged, the entry is picked up by the site build; check https://awesome-selfhosted.net (search "Hail") a day or two later to confirm it's live.

## Content

File: `software/hail.yml`

```yaml
name: "Hail"
website_url: "https://hail.so"
source_code_url: "https://github.com/hail-hq/hail"
description: "API for placing phone calls and sending email as an AI agent, via CLI, Python SDK, OpenAPI, or a remote MCP server."
licenses:
  - AGPL-3.0
platforms:
  - Python
  - Go
  - Docker
tags:
  - Communication - Custom Communication Systems
depends_3rdparty: true
```

Field notes:

- `name`: "Hail" (no suffix — check at submission time whether a name collision exists with any other already-listed "Hail"; rename the file/entry to disambiguate if needed, e.g. keep filename `hail.yml` but this is the only field maintainers would ask to change).
- `description`: sentence case, no "self-hosted"/"open-source" (redundant on this list), under 250 chars. Deliberately says "phone calls and email," not "phone, SMS, and email" — SMS is not shipped in any form today (see Notes), and this listing is a factual capability statement, not marketing copy.
- `licenses`: `AGPL-3.0` is the exact identifier used in `licenses.yml` (confirmed against an existing entry) — repo's own docs say "AGPL-3.0-or-later" but the data repo only has a single non-suffixed `AGPL-3.0` license slug.
- `platforms`: Python (core API, SDK, MCP server), Go (CLI), Docker (the `docker compose up` distribution path).
- `tags`: single tag, "Communication - Custom Communication Systems" — closest fit for a custom-protocol, API-first multi-channel comms system; no PBX/SIP-server tag fits (Hail isn't an IPBX) and no plain "Communication" catch-all tag exists.
- `depends_3rdparty: true` — accurate: even self-hosted, Hail needs Twilio + LiveKit Cloud (voice), AWS SES (email), Deepgram/Cartesia/ElevenLabs (voice pipeline), and an LLM vendor.
- No `demo_url` — Hail has no public interactive demo (it's an API/CLI, not a hosted app with a demo login).
- No `related_software_url` — no third-party plugin/app ecosystem to point to.
- No logo/icon field exists in this repo's schema; if a screenshot is wanted for the PR description (optional, not part of the YAML), use `/Users/r/playground/hail-website/public/assets/og-card-1200x630.png` or the wordmark at `/Users/r/playground/hail-website/public/assets/hail-wordmark.svg`.

## Notes

- **Eligibility gate, not a soft target:** the "first release > 4 months old" rule is enforced by maintainers reading the GitHub Releases page, not the CHANGELOG or git tags. `Hail v0.1.0` (published 2026-05-01T05:58:35Z, verified with `gh release list`) clears the bar on 2026-09-01. Submitting earlier risks an instant close via their canned "first release less than 4 months old" reply.
- **SMS deliberately left out of the description.** Checked against `README.md`'s own Milestones section and `core/hailhq/core/providers/` (only `voice/twilio.py` and `email/ses.py` exist): SMS outbound and inbound are both unchecked/unbuilt project-wide — this isn't a missing-vendor gap, it's a missing capability. Awesome-selfhosted maintainers do sometimes spot-check descriptions against the linked repo before merging, and a false "SMS" claim here is easy to disprove by opening the README. The drafted description claims only voice (outbound, Twilio, real and working) and email (in/out, AWS SES, real and working) — both true today. Revisit this file once an `sms/` provider actually ships.
- **Voice is outbound-only today** (inbound Twilio unchecked in Milestones) — "placing phone calls" in the description is accurate; avoid rewording to a bidirectional claim like "phone calls" without qualification if inbound isn't live yet by submission time.
- **Review turnaround:** awesome-selfhosted-data PRs are typically reviewed within a few days to ~2 weeks by volunteer maintainers; issues (non-PR route) can take longer since a maintainer has to do the YAML work themselves.
- **Contact used:** none yet — submission not filed. No account-specific contact info needed (public GitHub PR/issue).
- **Roadmap items not yet shipped, referenced above:** SMS (any vendor, outbound or inbound), inbound calls, Telnyx voice, Whisper/AssemblyAI STT, additional TTS vendors, recording/diarization — none affect this submission's YAML fields directly, but are why SMS is omitted from the description rather than marked "coming soon" inline (the field itself is a factual capability list, not a pitch).
