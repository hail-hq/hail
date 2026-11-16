---
target: "Cursor Marketplace"
slug: cursor-marketplace
category: mcp-registry
url: "https://cursor.com/marketplace/publish"
score: 8
status: drafted
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
- [ ] Submitted
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
- **Description:** Give your AI a phone number and an email inbox. Make calls, send texts, and handle email from Cursor.
- **Homepage:** `https://hail.so/mcp/cursor`
- **License:** AGPL-3.0-or-later
- **Support contact:** `hi@hail.so`
- **Test prompt:** "Find call-capable Hail numbers in the UK, France, and Canada. Compare monthly fees, setup fees, and registration requirements."

## Notes

- Checked against <https://cursor.com/docs/reference/plugins> on 2026-11-16.
  - Required manifest field: `name` (lowercase kebab-case). Ours: `hail`.
  - `displayName` is not in the docs field table, but Cursor's plugin template uses it.
  - Cursor also loads `skills/`, `rules/`, `agents/`, `commands/` and `hooks/hooks.json` at the plugin root by default. This repo has none of them, so the plugin contains only the MCP server.
- Not verified: the fields on the publish form (the page did not render for a script), the review time, and where to see review status. The docs say only "Plugins are reviewed by the Cursor team."
