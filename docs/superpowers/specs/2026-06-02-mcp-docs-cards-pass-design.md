# Docs + Website Cards — OAuth-First Pass Design

**Status:** Approved 2026-06-02. Follow-up to Phase 1c (`2026-06-02-mcp-resource-server-design.md`). Lands after 1c so the documented OAuth flow actually exists end-to-end.

## Problem

Hail's MCP setup docs and website client cards still lead with the static-key story: every `app/mcp/clients.ts` snippet hard-codes `Authorization: Bearer ${HAIL_API_KEY}`; `docs/setup/mcp.md` opens with "Authenticate with your `HAIL_API_KEY` as a bearer token"; the homepage `CodePanel.tsx` MCP tab carries an `mcp.json` with an embedded key. Phase 1c made the cloud MCP service an OAuth Resource Server — paste a URL, click Allow, no key. The user-facing surface now contradicts the deployed product.

This pass aligns the public-facing copy with the cloud-first reality. Cloud-OAuth leads; self-host stays first-class but secondary.

## Architecture

One pass, four files. No new files, no new dependencies, no logic changes.

**Cloud is the lead voice everywhere.** Each surface presents the URL-only OAuth flow as the primary path: paste `https://mcp.hail.so` into the client, click Allow in the browser, tools light up. The self-host path (static-key bearer against `http://localhost:8081`) collapses to a secondary disclosure — a footnote on the homepage, a notes-array line on each card, an H2 section in `mcp.md`.

The MCP service does not validate JWT signatures (single source of truth is `hail/api`); the docs do not surface that — operators reading the runbook do, end-users connecting their client do not need to. Authorized-apps management lands at `/console/apps`; every cloud-OAuth snippet ends with a one-line pointer there.

The four touchpoints:

1. **`hail/docs/setup/mcp.md`** — opening rewritten to URL-paste flow. New H2 _Self-host_ below cloud sections, pointing into `docs/operations.md`'s MCP-modes table for the env-var detail. Drop the SSE transition-window mention (1c removed it). No dedicated "Authorized apps" H2 here — that callout lives per-card in `clients.ts` only, per the spec's discoverability decision (cards are where users arrive after first-connect; mcp.md is reference material).
2. **`hail-website/app/mcp/clients.ts`** — all 8 cards rewritten OAuth-first. Per-client mechanism varies (UI vs CLI vs `mcp-remote` bridge) — the implementer reads each client's own MCP docs (the `docsUrl` already in each card) to author the right snippet. Two new lines in each card's `notes` array: the `/console/apps` pointer and the self-host fallback.
3. **`hail-website/app/components/CodePanel.tsx`** — homepage MCP tab swapped from `mcp.json`-with-key to URL-plus-narration. Three OAuth bullets, one self-host link footnote. Same tab key, same `<pre>` styling.
4. **`hail-website/app/mcp/page.tsx`** — one-line callout under the hero linking to `/console/apps` for first-connect manage-or-revoke.

## Per-client mechanism (clients.ts)

The implementer authors snippets from each client's own MCP documentation (already linked as `docsUrl` per card). Plan provides the structural template, not the verbatim command. Approximate per-client shape, subject to verification against each client's docs at write time:

| Client                                      | Path                | Snippet shape                                                                                                         |
| ------------------------------------------- | ------------------- | --------------------------------------------------------------------------------------------------------------------- |
| Claude.ai (Web)                             | Connectors UI       | "Settings → Connectors → Add custom connector → paste URL → Allow"                                                    |
| Claude Desktop                              | `mcp-remote` bridge | `mcp-remote https://mcp.hail.so` in `claude_desktop_config.json` — bridge handles OAuth; user sees consent in browser |
| Claude Code                                 | CLI                 | `claude mcp add --transport http hail https://mcp.hail.so` — CLI handles OAuth                                        |
| ChatGPT                                     | Connectors UI       | "Settings → Connectors → Add MCP server → paste URL → Allow"                                                          |
| Cursor                                      | Config file or UI   | URL entry; client negotiates OAuth on first connect                                                                   |
| Gemini / Windsurf / Copilot / Zed / Raycast | Per-client          | Implementer reads `docsUrl` to author                                                                                 |

Where a client lacks native MCP OAuth and `mcp-remote` is the bridge: snippet uses `mcp-remote URL` (no `--header`), and `mcp-remote` negotiates OAuth via the `WWW-Authenticate` 401 / discovery / DCR / consent / token chain that 1b + 1c make available.

## Components

`hail/docs/setup/mcp.md`. New structure:

```
# MCP clients
[opening — Hail speaks MCP, paste URL, click Allow]

## URL
- Hail Cloud: https://mcp.hail.so
- Self-host: http://<your-host>:8081 (see Self-host below)

## Tools
[unchanged table linking to mcp/hailhq/mcp/tools.py]

## Claude.ai (web)
[paste URL into Connectors UI; consent in browser]

## ChatGPT (web)
[paste URL into Connectors UI; consent in browser]

## Other clients
[link out to hail.so/mcp client picker]

## Self-host
[Bearer ${HAIL_API_KEY} against http://localhost:8081; link to docs/operations.md MCP-modes table for the env-var setup]
```

Existing content the rewrite keeps: the tools table verbatim, the "URL must be reachable" caveat for web-based clients (still true for cloud-OAuth — Claude.ai's servers fetch the URL). Drop entirely: the SSE transition-window paragraph, "Authenticate with your `HAIL_API_KEY` as a bearer token" (replaced by cloud-OAuth as default).

`hail-website/app/mcp/clients.ts`. The `McpClient` type stays; only `snippet`, `notes`, and possibly `method` change per card. Two new lines appended to every card's `notes` array:

```
"Manage or revoke this app at https://hail.so/console/apps after first connect.",
"Self-host? Set HAIL_API_KEY as the bearer; see docs/setup/mcp.",
```

Other type fields (`intro`, `verify`, `tag`, `glyph`, `surfaces`, `docsUrl`) are untouched per card — `intro` already sells the value-prop and didn't anchor on auth shape.

`hail-website/app/components/CodePanel.tsx`. The MCP tab's `<pre>` payload becomes:

```
$ # paste this into any MCP-aware client
https://mcp.hail.so

  ↳ click Allow on the consent screen
  ↳ your agent gets call/sms/mail tools
  ↳ no API keys, no install
  ↳ self-hosting? see hail.so/docs/setup/mcp
```

Same `cliRef` / `mcpRef` / `sdkRef` plumbing; same copy-button. The other two tabs (ZSH, AGENT.PY) are unchanged.

`hail-website/app/mcp/page.tsx`. One JSX paragraph added under the hero, before the grid section:

```jsx
<p className="hero-callout">
  After your first connection, manage authorized apps at{" "}
  <Link href="/console/apps">hail.so/console/apps</Link>.
</p>
```

Class name to match the existing hero typography — implementer reads `app/mcp/page.tsx` for the closest existing class (likely `lede` or similar) and reuses it.

## Out of scope

- Manual smoke testing each client's OAuth UX end-to-end. That's verification work; happens after this lands or in parallel.
- `app/mcp/[client]/page.tsx` (the dynamic per-client deep-link page, if one exists). The implementer audits during the plan — if the page already renders from `clients.ts`, the card rewrite covers it automatically.
- CSS / visual treatment changes on the MCP page.
- A first-OAuth-flow walkthrough tutorial.
- Updating `hail-website/app/components/CodePanel.tsx`'s other two tabs (ZSH, AGENT.PY). Those tabs are about the CLI and Python SDK, not MCP.

## Cross-cuts

- `MCP_RESOURCE_URL` in `.env.example` and the operator runbook (already in place from 1c) defines the cloud MCP URL: `https://mcp.hail.so`. The docs+cards pass uses this URL verbatim — if it ever changes, the docs change with it.
- Authorized-apps panel lives at `hail-website/app/console/apps/` (shipped in 1b). The console callout links there. If that path moves, the callout updates.
- The implementer should NOT need to invent any per-client command. Each card already carries a `docsUrl` to the client's own MCP documentation; that's the canonical source for the right command shape per client.
