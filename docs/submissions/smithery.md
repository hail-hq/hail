---
target: "Smithery"
slug: smithery
category: mcp-registry
url: "https://smithery.ai/docs/build/publish"
score: 8
status: drafted
---

# Smithery

## TODO

- [ ] Create/sign in to a Smithery account at smithery.ai
- [x] Confirm `https://mcp.hail.so` is publicly reachable over HTTPS and OAuth-gated — verified live 2026-07-07: `curl -i https://mcp.hail.so` returns `401` with `WWW-Authenticate: Bearer error="invalid_token", ..., resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"`, and that metadata URL resolves (`{"resource":"https://mcp.hail.so/","authorization_servers":["https://hail.so/api/auth"],"bearer_methods_supported":["header"]}`). Re-check if this drifts far from that date.
- [ ] Draft copy reviewed against the feature-claim policy
- [ ] Logo asset ready (`hail-monogram.svg` or `avatar-1024.png`) — path unverified in this checkout, `hail-website` is a separate repo not present here; confirm the file exists before upload
- [ ] Decide URL-paste (smithery.ai/new) vs. CLI publish (`smithery mcp publish`) — both are valid per Smithery's docs; default to URL-paste since it needs no local tooling
- [ ] Submitted (either path)
- [ ] Auto-scan completed successfully (tools/resources populated on the listing page) — if it stalls on the OAuth step, see the `server-card.json` fallback note below
- [ ] Confirmed live — record listing URL in Notes

## Steps to submit

**Path A — paste URL (default, no local tooling):**

1. Go to [smithery.ai/new](https://smithery.ai/new).
2. Sign in (GitHub login) if prompted — Smithery requires an account to own the listing.
3. Choose the "Bring your own hosting" / URL-based publish path (not the local MCPB bundle path — Hail's MCP server is remote-only, no local install; see "Why remote-only" in `docs/setup/mcp.md`).
4. Paste the public Streamable HTTP URL: `https://mcp.hail.so`
5. Before submitting, sanity-check OAuth discovery from a terminal (already confirmed once — see TODO — but re-run if time has passed):
   ```sh
   curl -i https://mcp.hail.so
   ```
   Expected: `401` with a `WWW-Authenticate: Bearer ... resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"` header. This is what Smithery's scanner uses to detect that the server is OAuth-gated (Hail Cloud runs in `oauth-rs` auth mode — see `mcp/hailhq/mcp/auth.py`). Smithery registers clients dynamically via Client ID Metadata Documents, so there's nothing to pre-register on Hail's side.
6. Let Smithery's auto-scan run. Because the server requires auth, Smithery will prompt you to authenticate through the same OAuth flow a normal client uses (paste URL → browser tab → click **Allow**) so the scanner can enumerate tools.
7. If auto-scan fails to complete the OAuth handshake (rather than falling back manually), note it — see the `server-card.json` item in **Notes**; Hail's MCP service does not currently serve that file, so there is no manual-metadata escape hatch today.
8. Once the scan completes, fill in the listing fields using the copy in **Content** below: name, one-liner, description, tags.
9. Upload the logo asset from **Content**.
10. Submit the listing for review.

**Path B — CLI publish (alternative, needs Node/npx):**

1. `npx -y @smithery/cli mcp publish "https://mcp.hail.so" -n @hail-hq/hail` (confirm the exact package/command name against current Smithery CLI docs before running — verify against https://smithery.ai/docs/build/publish since CLI syntax can drift).
2. This drives the same URL-based scan + OAuth flow as Path A from a terminal instead of the web form; finish remaining metadata (name, one-liner, tags, logo) on the resulting listing page using **Content** below.

**Either path — after submitting:** 11. After Smithery confirms the listing is live, update this file's frontmatter `status` to `submitted` and add the listing URL to **Notes**.

## Content

**Name:** Hail

**One-liner:** Phone, SMS & email — for agents.

**Description:**
Hail is a self-hostable, AGPLv3 communication platform built for AI agents. It gives an agent a real phone number, inbox, and messaging line — place and receive calls, send and receive email, and read back structured events — all through one remote MCP server. No local install: connect a client to `https://mcp.hail.so`, authorize once via OAuth, and the agent gets tools immediately. Self-hosters run the same server with a static API key instead of OAuth. Also available via CLI, a Python SDK, and OpenAPI for non-MCP integrations.

**Server URL (paste into Smithery):**

```
https://mcp.hail.so
```

**CLI publish command (alternative to pasting):**

```sh
npx -y @smithery/cli mcp publish "https://mcp.hail.so" -n @hail-hq/hail
```

**Transport:** Streamable HTTP (root path, no `/mcp` suffix, no SSE, no stdio)

**Auth:** OAuth 2.1 resource server. Unauthenticated calls return `401` with `WWW-Authenticate: Bearer resource_metadata=...`; Smithery's standard OAuth flow (paste URL, authorize in browser) applies. No client registration needed on Hail's side.

**Tags:** communication, voice, phone, sms, email, agents, self-hosted, open-source

**Tools exposed (per `mcp/hailhq/mcp/tools.py`, source of truth):**
| Tool | Does |
|---|---|
| `place_call` | Originate an outbound phone call |
| `send_email` | Send an outbound email |
| `get_call` | Fetch the current state of one call |
| `list_calls` | List recent calls (cursor-paginated) |
| `get_email` | Fetch one email's full record (body + inbound headers) |
| `list_emails` | List emails (`direction="inbound"` for replies) |
| `get_email_raw` | Presigned URL for an inbound email's raw MIME |
| `get_email_attachment` | Presigned URL for one inbound attachment |
| `get_email_events` | Page through an email's delivery/event history |
| `get_email_stats` | Aggregate email delivery/deliverability stats |
| `get_events` | Page through the account-wide event stream |

**License:** AGPLv3 — `github.com/hail-hq/hail`

**Logo asset:** `hail-website/public/assets/hail-monogram.svg` (square mark, use for the listing icon; `avatar-1024.png` as a raster fallback if SVG isn't accepted)

## Notes

- Auto-scan will discover the tool list live from `https://mcp.hail.so` — the table above is included for the human filling out the form, but Smithery's own scan is the actual source that populates the listing page.
- Smithery's docs describe an optional manual-metadata escape hatch at `/.well-known/mcp/server-card.json` for auth-protected servers where auto-scan can't complete. Checked directly: `https://mcp.hail.so/.well-known/mcp/server-card.json` currently 404s (`mcp/hailhq/mcp/server.py` only mounts `/.well-known/oauth-protected-resource`, which FastMCP auto-publishes for `oauth-rs` mode). If Smithery's OAuth-based auto-scan can't complete against Hail's flow, adding this file is the fallback — flag to engineering, don't build it speculatively for this submission.
- SMS is a shipped, present-tense capability of Hail as a whole (voice, SMS, email), but there is currently no `send_sms`/`list_sms` tool wired into the MCP tool surface (`mcp/hailhq/mcp/tools.py` exposes only call and email tools plus event/stats readers — 11 tools total, confirmed against the module's own docstring) — SMS-over-MCP is "coming soon." Don't claim SMS tools exist on this listing; the auto-scan would immediately contradict it.
- Voice is Twilio-backed, email is SES-backed (`core/hailhq/core/providers/voice/twilio.py`, `core/hailhq/core/providers/email/ses.py`) — those are the only provider adapters in `core/hailhq/core/providers/`, so don't name any other carrier/vendor in the listing copy.
- The logo path (`hail-website/public/assets/...`) follows the convention used across every other file in `docs/submissions/`, but `hail-website` is not part of this monorepo checkout — its existence/contents couldn't be verified from here. Confirm the actual filename before uploading.
- Review turnaround not documented by Smithery publicly; no stated SLA. Check the listing status directly on smithery.ai after submitting.
- No contact email used for this submission — flow is self-serve via GitHub login (Path A) or CLI (Path B).
