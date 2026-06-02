# Docs + Website Cards — OAuth-First Pass Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Align Hail's public-facing MCP docs and website cards with the cloud-OAuth-first reality shipped in Phase 1b + 1c. Cloud-OAuth leads everywhere; self-host static-key collapses to a secondary disclosure. No code changes; only copy, snippets, and one new JSX paragraph.

**Architecture:** Four files, no new files, no new dependencies. The per-client snippet content in `clients.ts` is authored from each client's own MCP documentation (already linked as `docsUrl` per card) — the plan provides the structural template, not the verbatim per-client commands. Snippet correctness is verified by reading the client's docs, not by smoke-testing each client (that's a separate verification step).

**Tech Stack:** Markdown (mcp.md, operations.md is already done), TypeScript (clients.ts is a typed const object), React/JSX (CodePanel.tsx, mcp/page.tsx). No tests; this is content-only.

**Spec:** `docs/superpowers/specs/2026-06-02-mcp-docs-cards-pass-design.md`.

---

## Pre-flight

Both repos have feature branches from the past two phases. This pass spans both repos.

In `hail/`:

```
cd /Users/r/playground/hail
git switch -c feat/mcp-docs-cards-pass
```

In `hail-website/`:

```
cd /Users/r/playground/hail-website
git switch -c feat/mcp-docs-cards-pass
```

Read the current state of all four touchpoint files before writing:

- `hail/docs/setup/mcp.md`
- `hail-website/app/mcp/clients.ts`
- `hail-website/app/components/CodePanel.tsx`
- `hail-website/app/mcp/page.tsx`

Also read `hail-website/app/mcp/[client]/page.tsx` if it exists — audit whether it renders from `clients.ts` (in which case the card rewrite covers it automatically) or whether it has its own per-client copy (in which case it needs touching too).

---

### Task 1: `hail/docs/setup/mcp.md` rewrite

**Files:**

- Modify: `hail/docs/setup/mcp.md`

The doc moves from "key-bearer first" to "URL-paste-and-click-Allow first". Self-host stays first-class but lives in a section below.

- [ ] **Step 1: Read the current file.**

```
cat /Users/r/playground/hail/docs/setup/mcp.md
```

Note: it currently has a "URL" section that mentions both self-host AND "Hail Cloud (later)" — that "later" is now wrong. Tools table is fine. SSE transition-window paragraph is wrong (1c removed SSE).

- [ ] **Step 2: Rewrite the file.**

Replace the entire content with:

```markdown
# MCP clients

Hail exposes MCP as a **remote server**. Cloud is OAuth: paste the URL into your client, click Allow in the browser, your agent gets call/sms/mail tools. No keys to manage, no install.

> Looking for the easy onboarding path? The [client picker on hail.so/mcp](https://hail.so/mcp) has copy-paste setup for the 8 most common clients (Claude.ai, ChatGPT, Cursor, Gemini, …). This page is the technical reference behind those snippets.

## URL

- **Hail Cloud**: `https://mcp.hail.so`
- **Self-hosted**: `http://<your-host>:8081` — see [Self-host](#self-host) below.

The Streamable HTTP transport serves the MCP root path; no `/mcp` suffix, no SSE.

> For web-based clients (Claude.ai, ChatGPT) the URL must be reachable from the client's servers — public DNS + TLS. Tunnel a self-hosted instance via cloudflared / tailscale funnel if you want it reachable to web clients.

## Tools

The server exposes five tools. Schemas (args, validation, return shapes) are the source of truth — see [`mcp/hailhq/mcp/tools.py`](../../mcp/hailhq/mcp/tools.py).

| Tool         | Does                                  |
| ------------ | ------------------------------------- |
| `place_call` | Originate an outbound phone call.     |
| `send_email` | Send an outbound email.               |
| `get_call`   | Fetch the current state of one call.  |
| `list_calls` | List recent calls (cursor-paginated). |
| `get_events` | Page through the event stream.        |

## Claude.ai (web)

1. **Settings → Connectors → Add custom connector**
2. Server URL: `https://mcp.hail.so`
3. Save. Claude prompts you to authorize on first use; click **Allow** in the browser tab that opens.

That's it. No API key field.

## ChatGPT (web)

1. **Settings → Connectors → Add MCP server**
2. Paste `https://mcp.hail.so`
3. Save. ChatGPT walks you through the OAuth consent on first call.

## Other clients

The [client picker](https://hail.so/mcp) has setup snippets for Cursor, Gemini, Windsurf, Copilot, Zed, Raycast, and Claude Desktop. All follow the same shape: paste URL, click Allow.

## Self-host

Self-hosters skip OAuth — set `HAIL_API_KEY` and send it as a bearer:
```

curl -H "Authorization: Bearer ${HAIL_API_KEY}" http://localhost:8081/

```

The MCP service picks its auth mode from env at boot — see the [MCP modes table in the operations runbook](../operations.md#mcp-modes) for the full env-var contract (`HAIL_AUTH_URL` for cloud, `HAIL_API_KEY` for self-host, mutually exclusive).
```

- [ ] **Step 3: Commit.**

```
cd /Users/r/playground/hail
git add docs/setup/mcp.md
git commit -m "$(printf 'docs(mcp): rewrite setup guide cloud-first\n\nOpening + URL section + Claude.ai/ChatGPT snippets now lead with the\npaste-URL-click-Allow OAuth flow shipped in Phase 1b+1c. Self-host\nstays first-class in its own section below, with the env-var contract\nlinking out to the operations runbook MCP-modes table. SSE\ntransition-window paragraph removed (1c dropped SSE entirely).\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 2: `hail-website/app/mcp/clients.ts` rewrite

**Files:**

- Modify: `hail-website/app/mcp/clients.ts`

All 8 client cards: snippet becomes OAuth-first; two new `notes` lines per card (console-apps pointer + self-host fallback). Per-client snippet shape requires reading each card's existing `docsUrl` to author correctly.

- [ ] **Step 1: Read the current file.**

```
cat /Users/r/playground/hail-website/app/mcp/clients.ts
```

Note the `McpClient` shape: `slug, name, tag, glyph, surfaces, intro, method, snippet { lang, file, code }, verify, notes, docsUrl`. Each card needs `snippet` and `notes` updated; everything else stays. Also note `MCP_CLIENT_ORDER`: claude, chatgpt, gemini, cursor, windsurf, copilot, zed, raycast. There's also an `MCP_ENDPOINT_URL` const at the top — confirm it's `https://mcp.hail.so`.

- [ ] **Step 2: For each of the 8 clients, fetch its MCP documentation to author the right snippet.**

The `docsUrl` field on each card is the canonical source. Fetch via `WebFetch` with a prompt like "What's the current way to add a remote Streamable-HTTP MCP server to <client>? Specifically I want the config snippet (file path + content) or the UI path. The server URL is https://mcp.hail.so and it accepts OAuth (the client should negotiate via the WWW-Authenticate hint on first 401)."

For each client, the snippet should:

- Use `https://mcp.hail.so` as the server URL (no `${HAIL_API_KEY}` env var, no `Authorization` header in the snippet)
- Point at the client's preferred config location (file path or settings UI sequence) — use the format the client actually documents
- Be copy-pasteable

Per-client expected shape (verify against current client docs):

| Client     | Likely snippet form                                                                                                                                                                     |
| ---------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `claude`   | `claude_desktop_config.json` with `mcp-remote https://mcp.hail.so` (no `--header`); the bridge negotiates OAuth. Connectors UI path also documented for Claude.ai web users in `notes`. |
| `chatgpt`  | Settings → Connectors → Add MCP server → paste URL. Snippet payload is the URL itself.                                                                                                  |
| `gemini`   | Verify against Gemini's MCP doc — may be config-file or UI.                                                                                                                             |
| `cursor`   | `~/.cursor/mcp.json` (or per-project `.cursor/mcp.json`) with a `mcpServers` entry pointing at the URL.                                                                                 |
| `windsurf` | Per Windsurf's MCP doc — likely a config file under `~/.codeium/`.                                                                                                                      |
| `copilot`  | VS Code MCP via Copilot — settings.json fragment.                                                                                                                                       |
| `zed`      | `~/.config/zed/settings.json` MCP fragment.                                                                                                                                             |
| `raycast`  | Settings UI path; URL-based MCP add.                                                                                                                                                    |

If any client's MCP support is too immature for OAuth-by-URL (e.g., only stdio-bridge works), the snippet uses `mcp-remote https://mcp.hail.so` (the bridge handles OAuth on the user's behalf). Document the reason in a notes entry.

- [ ] **Step 3: Author each card's new `snippet` and `notes`.**

For each card, replace `snippet.code` with the new OAuth-first content. Update `snippet.lang` and `snippet.file` to match if the format changed. Update `method` if the UI path changed (e.g., "Settings → Developer → Edit Config" → "Settings → Connectors → Add MCP server" for clients now offering UI-based MCP add).

Append two new entries to each card's `notes` array, in order:

```typescript
"Manage or revoke this app at https://hail.so/console/apps after first connect.",
"Self-host? Set HAIL_API_KEY as the bearer; see docs/setup/mcp.",
```

Keep existing notes that are still accurate (e.g., the Windows mcp-remote-header-mangling note on the Claude card can stay if Claude Desktop is still on mcp-remote; if it's now using a native MCP transport, drop that note).

The `intro`, `verify`, `tag`, `glyph`, `surfaces`, `docsUrl` fields are unchanged per card — `intro` sells the value-prop and didn't anchor on auth shape, `verify` is a sanity-check user prompt that's auth-agnostic.

- [ ] **Step 4: Typecheck.**

```
cd /Users/r/playground/hail-website && pnpm tsc --noEmit -p tsconfig.json
```

Expected: clean. If type errors, the `McpClient` shape probably needs the same fields it always had — verify no field was accidentally renamed.

- [ ] **Step 5: Audit `app/mcp/[client]/page.tsx`.**

If this file exists, check whether it renders from `MCP_CLIENTS` keyed by the slug (Task is done — the rewrite propagates automatically) or whether it has its own per-client copy that needs separate updating.

```
ls /Users/r/playground/hail-website/app/mcp/\[client\]/
```

If a non-shared per-client copy exists, audit each instance and propagate the same OAuth-first treatment. Surface as a sub-task if substantial.

- [ ] **Step 6: Commit.**

```
cd /Users/r/playground/hail-website
git add app/mcp/clients.ts
# also add app/mcp/[client]/page.tsx if Step 5 touched it
git commit -m "$(printf 'docs(mcp): rewrite client cards OAuth-first\n\nEvery card in app/mcp/clients.ts now leads with the paste-URL-click-Allow\nflow shipped in Phase 1b+1c. Per-client snippet content authored from\neach client current MCP documentation (linked as docsUrl per card).\nNotes array on every card gains pointers to hail.so/console/apps for\nrevoke management and to docs/setup/mcp for the self-host static-key\nfallback.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 3: `hail-website/app/components/CodePanel.tsx` MCP tab rewrite

**Files:**

- Modify: `hail-website/app/components/CodePanel.tsx`

Replace the MCP tab's `<pre>` payload with the URL-plus-narration block.

- [ ] **Step 1: Read the current file.**

```
cat /Users/r/playground/hail-website/app/components/CodePanel.tsx
```

Note the three tabs (`cli` / `mcp` / `sdk`) and their respective `<pre ref={...}>` blocks. The MCP one currently has a static-key `mcp.json` shape. Other tabs are out of scope.

- [ ] **Step 2: Replace the MCP `<pre>` content.**

The new payload (TypeScript / JSX representation — match the existing token-coloring spans `<span className="c">` / `"k"` / `"s"`):

```tsx
<pre ref={mcpRef} style={{ display: active === "mcp" ? undefined : "none" }}>
  <span className="c">$ # paste this into any MCP-aware client</span>
  {"\n"}
  <span className="k">https://mcp.hail.so</span>
  {"\n\n  ↳ "}
  <span className="s">click Allow on the consent screen</span>
  {"\n  ↳ "}
  <span className="s">your agent gets call/sms/mail tools</span>
  {"\n  ↳ "}
  <span className="s">no API keys, no install</span>
  {"\n  ↳ "}
  <span className="c">self-hosting? see hail.so/docs/setup/mcp</span>
</pre>
```

Use the same token-class conventions the surrounding code uses (the `c` / `k` / `s` classes show up in the ZSH tab — read it for the exact spans).

- [ ] **Step 3: Typecheck.**

```
cd /Users/r/playground/hail-website && pnpm tsc --noEmit -p tsconfig.json
```

Expected: clean.

- [ ] **Step 4: Boot dev server briefly + visually confirm.**

```
cd /Users/r/playground/hail-website && timeout 12s pnpm dev 2>&1 | tail -10 || true
```

Expected: `Ready`. Open `http://localhost:3000/` (or whichever port is free) — click the MCP.JSON tab, confirm the new payload renders, the copy button still works.

- [ ] **Step 5: Commit.**

```
cd /Users/r/playground/hail-website
git add app/components/CodePanel.tsx
git commit -m "$(printf 'feat(home): swap CodePanel MCP tab to OAuth-by-URL\n\nThe homepage MCP tab no longer carries an mcp.json with an embedded\nbearer. Single-URL paste + narration bullets match the cloud-OAuth\nflow shipped in 1c. Last bullet links self-hosters to the setup doc.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 4: `hail-website/app/mcp/page.tsx` hero callout

**Files:**

- Modify: `hail-website/app/mcp/page.tsx`

Add a one-line hero callout linking to `/console/apps`.

- [ ] **Step 1: Read the file + find the hero section.**

```
sed -n '1,60p' /Users/r/playground/hail-website/app/mcp/page.tsx
```

Look for the `<section className="mcp-idx-hero">` block. Find the existing `<p className="lede">` (the hero subtitle) — the new callout goes either inside the same wrap div, immediately below the lede, or directly below the section. Pick whichever placement preserves the existing visual hierarchy.

- [ ] **Step 2: Add the callout.**

Below the existing `<p className="lede">…</p>`, add:

```tsx
<p className="lede" style={{ marginTop: "0.5rem", opacity: 0.7 }}>
  After your first connection, manage authorized apps at{" "}
  <Link href="/console/apps">hail.so/console/apps</Link>.
</p>
```

If the file already imports `Link from "next/link"` (Task 1's reading of the file confirms), use it; otherwise add the import.

If the project has a more idiomatic "secondary lede" or "subnote" class in `globals.css`, prefer that over the inline `style={{...}}`. Read `app/globals.css` briefly to check; falling back to inline is fine.

- [ ] **Step 3: Typecheck + dev-server smoke.**

```
cd /Users/r/playground/hail-website && pnpm tsc --noEmit -p tsconfig.json
```

Expected: clean.

```
cd /Users/r/playground/hail-website && timeout 12s pnpm dev 2>&1 | tail -10 || true
```

Open `http://localhost:3000/mcp` — confirm the callout renders below the lede, the link goes to `/console/apps`.

- [ ] **Step 4: Commit.**

```
cd /Users/r/playground/hail-website
git add app/mcp/page.tsx
git commit -m "$(printf 'feat(mcp-page): link first-connect hero to authorized-apps console\n\nOne-line callout below the hero lede pointing users at\n/console/apps, where they can revoke authorized DCR clients after\ntheir first OAuth flow. Closes the loop on the 1b Authorized Apps\npanel.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

---

### Task 5: Cross-file consistency pass

**Files:** none — verification only.

After Tasks 1-4 land, sweep for residual contradictions:

- [ ] **Step 1: Grep for stale references.**

```
cd /Users/r/playground/hail && grep -rn "HAIL_API_KEY" docs/setup/mcp.md
cd /Users/r/playground/hail-website && grep -rn "HAIL_API_KEY\|mcp-remote.*--header" app/mcp/ app/components/CodePanel.tsx app/page.tsx
```

In `hail/`: `HAIL_API_KEY` should appear ONLY in the self-host section of `mcp.md` (zero false positives at the top of the file).

In `hail-website/`: `HAIL_API_KEY` should appear ONLY in the `notes` array of each card (the self-host pointer line) and nowhere in `snippet.code`. `mcp-remote.*--header` should not appear at all if every client supports native OAuth via `mcp-remote` URL-only; if any client legitimately needs the `--header` form (e.g., for self-hosters running through a tunnel), document the why in a notes entry.

- [ ] **Step 2: Grep for stale SSE mentions.**

```
cd /Users/r/playground/hail && grep -rn "/sse\|legacy SSE\|transition window\|/messages/" docs/setup/mcp.md
cd /Users/r/playground/hail-website && grep -rn "/sse\|legacy SSE" app/mcp/
```

Expected: no matches in either repo. 1c removed SSE; the docs+cards pass must remove the mentions.

- [ ] **Step 3: Verify the homepage CodePanel doesn't reference a key.**

```
cd /Users/r/playground/hail-website && grep -n "HAIL_API_KEY\|hl_live" app/components/CodePanel.tsx
```

Expected: no matches in the MCP tab. (The ZSH or AGENT.PY tabs may still reference keys legitimately — those tabs are out of scope.)

- [ ] **Step 4: If any stale references found, fix them in a follow-up commit on the same branch.**

```
git add <files>
git commit -m "$(printf 'docs(mcp): scrub residual static-key references\n\nFollow-up consistency pass on the docs+cards OAuth-first rewrite.\n\nCo-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>')"
```

- [ ] **Step 5: Final review handoff.**

After all five tasks: dispatch the code-quality reviewer over the whole diff in each repo, then run `superpowers:finishing-a-development-branch` for each branch.

---

## Self-Review

**Spec coverage** (every load-bearing item from `docs/superpowers/specs/2026-06-02-mcp-docs-cards-pass-design.md`):

- Cloud-OAuth leads in `mcp.md` opening + URL section + Claude.ai/ChatGPT snippets → Task 1.
- Self-host H2 in `mcp.md` linking to `docs/operations.md` MCP-modes table → Task 1.
- SSE transition-window paragraph dropped → Task 1.
- All 8 cards rewritten OAuth-first; per-client snippet authored from each card's `docsUrl` → Task 2.
- Two new `notes` lines on every card (console-apps pointer + self-host fallback) → Task 2.
- Per-client deep-link page audit → Task 2 Step 5.
- Homepage CodePanel MCP tab swapped to URL-paste + narration → Task 3.
- `/mcp` page hero callout linking to `/console/apps` → Task 4.
- Cross-file consistency sweep → Task 5.

**Placeholder scan:** No TBDs. The per-client snippet table in Task 2 Step 2 marks each shape as "Likely … (verify against current client docs)" — that's an _instruction_ to verify, not a deferred decision. The implementer fetches the canonical doc via `WebFetch` against each card's `docsUrl`.

**Type / name consistency:**

- `MCP_CLIENT_ORDER` slugs (`claude`, `chatgpt`, `gemini`, `cursor`, `windsurf`, `copilot`, `zed`, `raycast`) match the table in Task 2 Step 2.
- `McpClient` type unchanged (only field _values_ change per card).
- `MCP_ENDPOINT_URL` used verbatim across mcp.md, every snippet, and the CodePanel block.
- The `notes` array additions use the exact strings declared in the spec.

**Known follow-ups (NOT in this plan):**

- Manual smoke against each client's OAuth UX end-to-end (Cursor, Gemini, Windsurf, etc.). Verification work; happens after this lands.
- A first-OAuth-flow walkthrough tutorial / animated demo. Marketing-leaning; future plan if needed.
- CSS / visual treatment changes to the `/mcp` page. Out of this pass.
