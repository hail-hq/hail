---
target: "Hermes Agent MCP catalog (Nous Research)"
slug: hermes-mcp-catalog
category: mcp-registry
url: "https://github.com/NousResearch/hermes-agent/tree/main/optional-mcps"
score: 7
status: drafted
---

# Hermes Agent MCP catalog (Nous Research)

Hermes Agent lists approved MCP servers in `optional-mcps/` of `NousResearch/hermes-agent`. Users find them with `hermes mcp catalog` and add one with `hermes mcp install <name>`. The setup guide is <https://hail.so/mcp/hermes>.

The Hail skill does not need a Hermes listing. Hermes installs skills from ClawHub, where Hail is listed (see `clawhub.md`), and from any `SKILL.md` URL: `hermes skills install https://hail.so/skill.md`.

## TODO

- [x] Manifest drafted (below)
- [ ] Owner approves opening a PR in `NousResearch/hermes-agent`
- [ ] PR opened
- [ ] Merged: `hermes mcp install hail` works

## Steps to submit

From <https://hermes-agent.nousresearch.com/docs/user-guide/features/mcp>, read 2026-11-18: "To add an MCP to the catalog, open a PR against `optional-mcps/`." "There is no community submission tier; entries are added by merging a PR."

1. Fork `NousResearch/hermes-agent` and branch from `main`.
2. Add `optional-mcps/hail/manifest.yaml` with the manifest from **Content**.
3. Commit with a Conventional Commits title: `feat(optional-mcps): add hail`.
4. Open the PR against `main`. Use the PR text from **Content**.
5. Record the PR URL in **Notes**.

## Content

`optional-mcps/hail/manifest.yaml` (same shape as the `linear` entry, which also uses OAuth with Dynamic Client Registration):

```yaml
# Nous-approved MCP catalog entry.
# Presence in this directory = approval. Merged via PR review.
manifest_version: 1

name: hail
description: Handle calls, texts, and email from a real phone number and email inbox.
source: https://hail.so/mcp/hermes

# Hail runs a remote MCP server over Streamable HTTP with OAuth 2.1 and
# Dynamic Client Registration. Users sign in to a Hail account in the browser.
transport:
  type: http
  url: https://mcp.hail.so

auth:
  type: oauth

suggest:
  keywords:
    - hail
    - phone number
    - text message
    - email inbox
  hosts:
    - hail.so

post_install: |
  On first connection, Hermes opens a browser to sign in to Hail.
  After you sign in, restart your Hermes session so the Hail tools load.

  Hail asks for consent on each call, text, and email you send.
  Buying a number charges your Hail credit.
```

PR title: `feat(optional-mcps): add hail`

PR text:

> Adds Hail to the MCP catalog. Hail gives your AI assistant a real phone number and an email inbox, so Hermes can handle calls, texts, and email.
>
> - Remote server: `https://mcp.hail.so`, Streamable HTTP, OAuth 2.1 with Dynamic Client Registration (same shape as `linear`).
> - Setup guide: https://hail.so/mcp/hermes
> - Source: https://github.com/hail-hq/hail (AGPL-3.0). Listed in the official MCP Registry as `io.github.hail-hq/hail-mcp`.
>
> Submitted by the Hail maintainers.

## Notes

- 2026-11-18: about a dozen other `feat(optional-mcps)` PRs were open and waiting for review, the oldest from 2026-11-06.
- The manifest fields come from the `linear` and `rentahuman` entries. The repo has no published schema for them.
