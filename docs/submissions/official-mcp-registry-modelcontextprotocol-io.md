---
target: "Official MCP Registry (modelcontextprotocol.io)"
slug: official-mcp-registry-modelcontextprotocol-io
category: mcp-registry
url: "https://github.com/modelcontextprotocol/registry"
score: 9.3
status: submitted
---

# Official MCP Registry (modelcontextprotocol.io)

## TODO

- [x] Decide auth path: **GitHub org (`hail-hq`)** — confirmed `r13i` is already a member (`gh api orgs/hail-hq/members/r13i` → 204), faster than DNS, no zone access needed. Name is `io.github.hail-hq/hail-mcp`.
- [x] Install `mcp-publisher` CLI locally (`brew install mcp-publisher`, v1.7.9)
- [x] `server.json` content reviewed against feature-claim policy — see Notes
- [x] Confirm `https://mcp.hail.so` still returns `401` + `WWW-Authenticate: Bearer ... resource_metadata=...` (re-verified 2026-07-07)
- [x] `mcp-publisher validate` passes locally — **corrected 2026-07-07: original draft description (222 chars) exceeded the schema's 100-char hard limit** (`expected length <= 100`, caught by real validation, not previously known). Shortened to 86 chars, re-validated clean. `server.json` ready at `/Users/r/playground/hail-mcp-publish/server.json`.
- [x] Domain or GitHub org verification completed, `mcp-publisher login github` succeeded
- [x] `mcp-publisher publish` run — `✓ Successfully published, Server io.github.hail-hq/hail-mcp version 0.1.0`
- [x] Confirmed live 2026-07-07: `GET https://registry.modelcontextprotocol.io/v0.1/servers?search=hail-mcp` returns the listing, `status: active`, `publishedAt: 2026-07-07T13:41:48Z`

## Steps to submit

This target has no web form — it's a CLI publish flow (`mcp-publisher`) against a `server.json` manifest, gated by proving you own either the `hail.so` domain or the `hail-hq` GitHub org. We don't hold your DNS or GitHub credentials, so you run the auth steps yourself.

### 1. Install the CLI

```bash
brew install mcp-publisher
```

(or download a prebuilt binary from the [latest release](https://github.com/modelcontextprotocol/registry/releases/latest) if you're not on Homebrew.)

Verify:

```bash
mcp-publisher --help
```

### 2. Write `server.json`

In a scratch directory (doesn't need to be inside the `hail` repo — the manifest just points at the public GitHub repo and the public MCP URL), create `server.json` with the **exact contents from the Content section below**.

### 3. Validate it locally

```bash
mcp-publisher validate server.json
```

Fix anything it flags before continuing.

### 4. Sanity-check the remote is publicly reachable

The registry requires the `remotes[].url` to be publicly reachable. It doesn't need to return 200 — Hail's MCP server is OAuth-protected, so an unauthenticated request correctly returns `401` with a `WWW-Authenticate` header pointing at its protected-resource metadata. Confirm you see that shape (not a connection error or DNS failure):

```bash
curl -i https://mcp.hail.so/
# expect: HTTP/2 401, with a www-authenticate header containing
# resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"
```

### 5. Authenticate — pick ONE path

**Decided 2026-07-07: Path B (GitHub org).** `r13i` is a confirmed `hail-hq` member — no DNS zone access needed, and it's the faster path. `server.json` above already uses the resulting `io.github.hail-hq/hail-mcp` name. Only remaining action, from `/Users/r/playground/hail-mcp-publish/`:

```bash
mcp-publisher login github
# follow the printed device-flow prompt — open the URL, enter the code, click Authorize
mcp-publisher publish
```

This needs a human to click "Authorize" in a browser — not automatable. Path A (DNS) below is kept for reference only; not the path being used.

**Path A — DNS verification on `hail.so` (reference only, not being used — see decision above).**

Requires OpenSSL 3+ for the Ed25519 path. On macOS the system `openssl` is LibreSSL and doesn't support Ed25519 `genpkey` — install `brew install openssl@3` and call it explicitly (paths below).

```bash
MY_DOMAIN="hail.so"
OPENSSL=/opt/homebrew/opt/openssl@3/bin/openssl   # Intel Mac: /usr/local/opt/openssl@3/bin/openssl

# 1. Generate a keypair
$OPENSSL genpkey -algorithm Ed25519 -out key.pem

# 2. Derive the TXT record value
PUBLIC_KEY="$($OPENSSL pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "${MY_DOMAIN}. IN TXT \"v=MCPv1; k=ed25519; p=${PUBLIC_KEY}\""
```

3. Add that TXT record **at the apex of `hail.so`** (not `mcp.hail.so`, not `_mcp-auth.hail.so` — the bare apex, SPF-style placement) via whatever DNS provider hosts `hail.so`'s zone. Wait for propagation (`dig TXT hail.so` until it shows up — can take a few minutes).

4. Log in:

```bash
PRIVATE_KEY="$($OPENSSL pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
mcp-publisher login dns --domain "${MY_DOMAIN}" --private-key "${PRIVATE_KEY}"
```

5. If you rotate this key later, remove the old TXT record from the apex — a stale one left behind is tried first and breaks verification.

**Path B — GitHub org verification on `hail-hq` (faster, no DNS/key handling, name becomes `io.github.hail-hq/hail-mcp`).**

```bash
mcp-publisher login github
```

Follow the printed device-flow prompt: open `https://github.com/login/device`, enter the code, click **Authorize** while signed in as a member/owner of the `hail-hq` org.

> If you use Path B, edit `server.json`'s `name` field to `io.github.hail-hq/hail-mcp` before publishing — the name's namespace prefix must match whichever auth method you used.

### 6. Publish

```bash
mcp-publisher publish
```

Expect `✓ Successfully published` and the server name/version echoed back.

### 7. Confirm it's live

```bash
curl "https://registry.modelcontextprotocol.io/v0.1/servers?search=hail-mcp"
```

Confirm the listing appears, then check it renders on the registry's own site.

### 8. Update the tracker

Flip `status: submitted` (then `submitted (live)`) in this file's frontmatter and in `docs/submissions/README.md` once confirmed.

## Content

**One-liner** (for `description` / anywhere a short pitch is needed):

> Phone, SMS & email — for agents. One remote MCP endpoint, OAuth login, zero install.

**Longer description** (for `websiteUrl` context / any registry aggregator that mirrors this listing with more room):

> Hail is a self-hostable, AGPLv3 communication platform for AI agents — voice calls, SMS, and email, all reachable from one remote MCP server. No stdio, no local install, no API keys to juggle: paste `https://mcp.hail.so` into your client, click Allow, your agent gets call/email tools with OAuth-scoped access. Self-host the whole stack if you'd rather not touch Hail Cloud.

**`server.json`** — **corrected 2026-07-07: using GitHub-org naming (Path B), and a shortened description (the original 222-char version fails real schema validation — 100-char hard limit).** Ready, validated file at `/Users/r/playground/hail-mcp-publish/server.json`:

```json
{
  "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
  "name": "io.github.hail-hq/hail-mcp",
  "title": "Hail",
  "description": "Phone, SMS & email for AI agents — one remote MCP endpoint, OAuth login, zero install.",
  "websiteUrl": "https://hail.so/mcp",
  "repository": {
    "url": "https://github.com/hail-hq/hail",
    "source": "github",
    "subfolder": "mcp"
  },
  "version": "0.1.0",
  "icons": [
    {
      "src": "https://hail.so/assets/monogram-512.png",
      "sizes": ["512x512"],
      "mimeType": "image/png"
    },
    {
      "src": "https://hail.so/assets/monogram-1024.png",
      "sizes": ["1024x1024"],
      "mimeType": "image/png"
    }
  ],
  "remotes": [
    {
      "type": "streamable-http",
      "url": "https://mcp.hail.so"
    }
  ]
}
```

Asset source paths (already mirrored, confirmed resolving live at the URLs above): `hail-website/public/assets/monogram-512.png`, `hail-website/public/assets/monogram-1024.png`.

**Tool inventory** (for reference / any listing field that asks what the server does — not a `server.json` field, the schema has no tags/tools array; this is the literal, code-verified list from `mcp/hailhq/mcp/tools.py`, 11 tools):

| Tool                   | Does                                                               |
| ---------------------- | ------------------------------------------------------------------ |
| `place_call`           | Originate an outbound phone call.                                  |
| `send_email`           | Send an outbound email.                                            |
| `get_call`             | Fetch the current state of one call.                               |
| `list_calls`           | List recent calls, cursor-paginated.                               |
| `get_email`            | Fetch one email's full record (body + inbound headers/verdicts).   |
| `list_emails`          | List emails; `direction="inbound"` for replies.                    |
| `get_email_raw`        | Presigned URL for an inbound email's raw MIME source.              |
| `get_email_attachment` | Presigned URL for one inbound attachment.                          |
| `get_email_events`     | Per-message delivery/engagement timeline (sent→delivered→opened…). |
| `get_email_stats`      | Account-level deliverability stats (rates, time series).           |
| `get_events`           | Page through the org- or call-level event stream.                  |

**Install/usage snippet** (what a reader clicks through after finding the listing):

```
Server URL: https://mcp.hail.so
Auth: OAuth (click Allow when your client prompts) — cloud
      Bearer HAIL_API_KEY — self-host
```

## Notes

- **Namespace choice is a one-way door per name.** Once published under `so.hail/hail-mcp` (or `io.github.hail-hq/hail-mcp`), later switching auth methods means publishing under a _new_ name — the old listing doesn't migrate. Decide DNS vs. GitHub before running step 5, not after.
- **SMS is not yet an MCP tool.** `core/hailhq/core/providers/` currently wires up voice and email only; SMS is an approved-but-unimplemented spec (`docs/superpowers/specs/2026-07-06-sms-support-design.md`). The one-liner/description above name SMS as a Hail capability per the team's stated positioning call (core-capability claims are written present-tense regardless of today's milestone state — see `docs/superpowers/specs/2026-07-06-registry-submissions-design.md`), but the `server.json` tool inventory only lists what's actually callable today. Once SMS tools land in `mcp/hailhq/mcp/tools.py`, bump `version` and republish (`mcp-publisher publish` again with the same `name`, higher `version`).
- **Self-host isn't listed as a `packages` entry on purpose.** There's a public image at `ghcr.io/hail-hq/hail-mcp:latest` (GHCR is an allowlisted OCI host for this registry), but it's a mutable `:latest`/`sha-<commit>` deploy artifact, not a versioned release — doesn't map cleanly to the registry's per-version package model. This matches Hail's deliberate remote-only MCP distribution stance (see "Why remote-only" in `docs/setup/mcp.md`); the listing is the remote endpoint only.
- **Reachability check caveat:** `https://mcp.hail.so/` correctly 401s without a bearer token (OAuth-protected resource) — that's expected and is what "publicly accessible" means here, not an open 200. Verified live 2026-07-06.
- **Registry is in preview** per its own docs — expect possible schema/breaking changes; `mcp-publisher validate` will flag deprecated schema versions if the pinned `$schema` URL above drifts.
- Review turnaround: none — publish is synchronous and self-service, no moderation queue observed in the docs.
- **2026-07-07 update:** `description` has a hard 100-char limit enforced by real server-side validation (`mcp-publisher validate` → 422 `expected length <= 100`) — not documented anywhere we found ahead of time, only caught by actually running validation. Shortened from 222 to 86 chars. If this field is ever expanded, re-validate before publishing rather than trusting the length looks "about right."
- **2026-07-07 update:** confirmed `r13i` is a `hail-hq` org member (`gh api orgs/hail-hq/members/r13i` → 204), so Path B (GitHub) was used instead of Path A (DNS) — no DNS zone access was needed after all. `server.json` ready and validated at `/Users/r/playground/hail-mcp-publish/server.json`. Only remaining step is `mcp-publisher login github` + `mcp-publisher publish`, which needs a human to complete the device-flow browser authorization.
