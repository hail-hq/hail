---
target: "Cursor Marketplace"
slug: cursor-marketplace
category: mcp-registry
url: "https://cursor.com/marketplace/publish"
score: 8
status: submitted
---

# Cursor Marketplace

The official Cursor plugin marketplace at <https://cursor.com/marketplace>.
It is not the community site `cursor.directory` (see `cursor-directory-community.md`).

## TODO

- [x] Plugin manifest at the repo root: `.cursor-plugin/plugin.json`
- [x] MCP config: `mcp.json` (remote server `https://mcp.hail.so`, OAuth, no secrets)
- [x] Logo committed and referenced by a relative path: `docs/assets/hail-messenger.svg`
- [x] README documents setup: the "Cursor plugin" section of `README.md`
- [ ] Plugin tested locally in Cursor (steps below), with one tool call that works
- [x] Submitted (owner, before 2026-11-20; no review result yet). Submitted before the local test, so if Cursor rejects it for a failing tool call, run the local test first.
- [ ] Confirmed live

## Steps to submit

1. Test the plugin locally. From a checkout of `main`, run the commands in the "Cursor plugin" section of `README.md`.
2. Reload Cursor. Open Customize and check that the Hail MCP server is listed.
3. Send the test prompt from the README. Sign in to Hail when Cursor asks. Check that a tool returns numbers.
4. Open <https://cursor.com/marketplace/publish> and sign in with your Cursor account.
5. Enter the repository URL from **Content** below. Fill the other fields from **Content**.
6. Submit. Record the date and the listing URL in **Notes**.

## Content

- **Repository:** `https://github.com/hail-hq/hail`
- **Plugin name:** `hail`
- **Display name:** Hail
- **Description:** Give your AI a real phone number and an email inbox. Handle calls, texts, and email in Cursor.
- **Homepage:** `https://hail.so/mcp/cursor`
- **License:** AGPL-3.0-or-later
- **Support contact:** `hi@hail.so`
- **Test prompt:** "Find call-capable Hail numbers in the UK, France, and Canada. Compare monthly fees, setup fees, and registration requirements."

## Notes

- Checked against <https://cursor.com/docs/reference/plugins> and <https://cursor.com/docs/plugins> on 2026-11-18. The docs say only: "Go to cursor.com/marketplace/publish and submit your repository link." "All plugins must be open source." Every update is reviewed again before it is published.
- The publish form does not render for a script (checked 2026-11-18), so its fields and limits are not verified. Fill it from **Content**.
  - Required manifest field: `name` (lowercase kebab-case). Ours: `hail`.
  - `displayName` is not in the docs field table, but Cursor's plugin template uses it.
  - Cursor also loads `skills/`, `rules/`, `agents/`, `commands/` and `hooks/hooks.json` at the plugin root by default. This repo has none of them, so the plugin contains only the MCP server.
- 2026-11-20: checked the repo against Cursor's submission checklist. The manifest, `name`, `description`, `mcp.json`, the logo (committed, relative path), `README.md`, and relative paths all pass. No `${VAR}` and no `skills/`, `rules/`, `agents/`, `commands/` or `hooks/` folders at the root. Not a multi-plugin repo, so no `marketplace.json`. The local test is the owner's step.
- The publish form is a publisher application: organization name, handle, contact email, logotype URL (1:1 SVG or PNG with a background plate), description, GitHub repository, owner, website URL. Without a paid Cursor team, the owner is an individual account.
- Not verified: the review time, and where to see review status. The docs say only "Plugins are reviewed by the Cursor team."
