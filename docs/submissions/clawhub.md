---
target: "ClawHub (OpenClaw skills registry)"
slug: clawhub
category: mcp-registry
url: "https://clawhub.ai"
score: 7
status: submitted
---

# ClawHub (OpenClaw skills registry)

ClawHub lists skills for OpenClaw. Hermes Agent also installs skills from it. The Hail skill comes from <https://hail.so/skill.md>.

## TODO

- [x] Listed (found 2026-11-18)
- [ ] Decide whether to keep both entries

## Steps to submit

Nothing to submit. The skill is live. Publishing steps are in the website repo, `docs/agent-skill-publishing.md`.

## Content

- `hail-hq/hail`, "Hail Communication": <https://clawhub.ai/hail-hq/skills/hail>
- `r13i/hail`, "Hail Skill": <https://clawhub.ai/r13i/skills/hail>

## Notes

- Found with the ClawHub search API (`https://clawhub.ai/api/v1/search?q=hail`) on 2026-11-18. Downloads then: 200 for `hail-hq/hail`, 250 for `r13i/hail`.
- Two entries split the downloads and the search ranking. Keeping only `hail-hq/hail` would point every user to the organization.
