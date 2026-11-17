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

- [x] Decide auth path: **GitHub org (`hail-hq`)**. Confirmed `r13i` is already a member (`gh api orgs/hail-hq/members/r13i` returns 204), faster than DNS, no zone access needed. Name is `io.github.hail-hq/hail-mcp`.
- [x] Install `mcp-publisher` CLI locally (`brew install mcp-publisher`, v1.7.9)
- [x] `server.json` content reviewed against feature-claim policy (see Notes)
- [x] Confirm `https://mcp.hail.so` still returns `401` + `WWW-Authenticate: Bearer ... resource_metadata=...` (re-verified 2026-07-07)
- [x] `mcp-publisher validate` passes locally. **Corrected 2026-07-07: original draft description (222 chars) exceeded the schema's 100-char hard limit** (`expected length <= 100`, caught by real validation, not previously known). Shortened to 86 chars, re-validated clean. `server.json` lives at `mcp/server.json` in this repo.
- [x] Domain or GitHub org verification completed, `mcp-publisher login github` succeeded
- [x] `mcp-publisher publish` run: `✓ Successfully published, Server io.github.hail-hq/hail-mcp version 0.1.0`
- [x] Confirmed live 2026-07-07: `GET https://registry.modelcontextprotocol.io/v0.1/servers?search=hail-mcp` returns the listing, `status: active`, `publishedAt: 2026-07-07T13:41:48Z`
- [x] 2026-10-18: version 0.1.1 published (description without a dash). The laptop login flow now gets 403 for the org namespace, see step 5. Publishing moved to `.github/workflows/mcp-publish.yml`.

## Steps to submit

This target has no web form. It is a CLI publish flow (`mcp-publisher`) against a `server.json` manifest, gated by proving you own either the `hail.so` domain or the `hail-hq` GitHub org. We don't hold your DNS or GitHub credentials, so you run the auth steps yourself.

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

In a scratch directory (it does not need to be inside the `hail` repo, because the manifest just points at the public GitHub repo and the public MCP URL), create `server.json` with the **exact contents from the Content section below**.

### 3. Validate it locally

```bash
mcp-publisher validate server.json
```

Fix anything it flags before continuing.

### 4. Sanity-check the remote is publicly reachable

The registry requires the `remotes[].url` to be publicly reachable. It doesn't need to return 200. Hail's MCP server is OAuth-protected, so an unauthenticated request correctly returns `401` with a `WWW-Authenticate` header pointing at its protected-resource metadata. Confirm you see that shape (not a connection error or DNS failure):

```bash
curl -i https://mcp.hail.so/
# expect: HTTP/2 401, with a www-authenticate header containing
# resource_metadata="https://mcp.hail.so/.well-known/oauth-protected-resource"
```

### 5. Authenticate and publish: GitHub Actions (current path)

**Decided 2026-10-18.** `.github/workflows/mcp-publish.yml` publishes `mcp/server.json` on every merge to `main` that changes that file, and can be run by hand from the Actions tab. It logs in with `mcp-publisher login github-oidc`, which grants `io.github.hail-hq/*` from the repository owner. No token, no browser click.

To publish a change: edit `mcp/server.json`, bump `version` (the registry refuses a version it already has), merge. Check the run under Actions, then confirm with step 7.

Why not the laptop login any more: since 2026-10 the registry grants an org namespace only to org Owners, read through `GET /user/memberships/orgs` with the `read:org` scope. The token from `mcp-publisher login github` does not see the `hail-hq` org (verified 2026-10-18: publish returns 403 "You have permission to publish: io.github.r13i/\*"), even after granting the OAuth app org access. A `gh auth token` does see it. Manual fallback, from `mcp/`, if the workflow is ever unavailable:

```bash
T=$(gh auth token)
J=$(curl -s -X POST https://registry.modelcontextprotocol.io/v0/auth/github-at \
  -H 'Content-Type: application/json' -d "{\"github_token\":\"$T\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['registry_token'])")
python3 -c "import json,os,sys;p=os.path.expanduser('~/.config/mcp-publisher/token.json');json.dump({'method':'github','registry':'https://registry.modelcontextprotocol.io','token':sys.argv[1]},open(p,'w'))" "$J"
mcp-publisher publish
```

The registry token lasts 5 minutes. Paths A and B below are the original 2026-07 setup, kept for reference only.

**Path A: DNS verification on `hail.so` (reference only, not being used, see decision above).**

Requires OpenSSL 3+ for the Ed25519 path. On macOS the system `openssl` is LibreSSL and doesn't support Ed25519 `genpkey`. Install `brew install openssl@3` and call it explicitly (paths below).

```bash
MY_DOMAIN="hail.so"
OPENSSL=/opt/homebrew/opt/openssl@3/bin/openssl   # Intel Mac: /usr/local/opt/openssl@3/bin/openssl

# 1. Generate a keypair
$OPENSSL genpkey -algorithm Ed25519 -out key.pem

# 2. Derive the TXT record value
PUBLIC_KEY="$($OPENSSL pkey -in key.pem -pubout -outform DER | tail -c 32 | base64)"
echo "${MY_DOMAIN}. IN TXT \"v=MCPv1; k=ed25519; p=${PUBLIC_KEY}\""
```

3. Add that TXT record **at the apex of `hail.so`** (not `mcp.hail.so`, not `_mcp-auth.hail.so`, but the bare apex, SPF-style placement) via whatever DNS provider hosts `hail.so`'s zone. Wait for propagation (`dig TXT hail.so` until it shows up, which can take a few minutes).

4. Log in:

```bash
PRIVATE_KEY="$($OPENSSL pkey -in key.pem -noout -text | grep -A3 "priv:" | tail -n +2 | tr -d ' :\n')"
mcp-publisher login dns --domain "${MY_DOMAIN}" --private-key "${PRIVATE_KEY}"
```

5. If you rotate this key later, remove the old TXT record from the apex. A stale one left behind is tried first and breaks verification.

**Path B: GitHub org verification on `hail-hq` (faster, no DNS/key handling, name becomes `io.github.hail-hq/hail-mcp`).**

```bash
mcp-publisher login github
```

Follow the printed device-flow prompt: open `https://github.com/login/device`, enter the code, click **Authorize** while signed in as a member/owner of the `hail-hq` org.

> If you use Path B, edit `server.json`'s `name` field to `io.github.hail-hq/hail-mcp` before publishing. The name's namespace prefix must match whichever auth method you used.

### 6. Publish

Merge the `server.json` change (step 5) and watch the `mcp-publish` run. Expect `✓ Successfully published` and the server name/version echoed back in the job log.

### 7. Confirm it's live

```bash
curl "https://registry.modelcontextprotocol.io/v0/servers/io.github.hail-hq%2Fhail-mcp/versions/latest"
```

The `?search=` endpoint sometimes hangs; the versions endpoint answers at once.

Confirm the listing appears, then check it renders on the registry's own site.

### 8. Update the tracker

Flip `status: submitted` (then `submitted (live)`) in this file's frontmatter and in `docs/submissions/README.md` once confirmed.

## Content

**One-liner** (for `description` / anywhere a short pitch is needed):

<!-- copy:short -->

> A real phone number and email inbox for your AI agent. Calls, texts, and email in one place.

<!-- /copy -->

**Longer description** (for `websiteUrl` context / any registry aggregator that mirrors this listing with more room):

<!-- copy:long -->

> Hail gives your AI agent a real phone number and an email inbox. Your agent can do any work people do on the phone and in email.
>
> Use it as an AI phone agent or an AI answering service. It handles calls, texts (SMS), and email day and night, and passes urgent calls to a person.
>
> Hail keeps the whole conversation. When someone calls after an email, your agent already knows what was said.
>
> Get a local or toll-free number in 120+ countries. Hail helps with the paperwork some countries need, such as US A2P 10DLC registration for texts.
>
> Connect Hail to Claude, ChatGPT, Cursor, or another MCP client in minutes, with nothing to install. Or build it into your app with the API, CLI, or Python SDK.
>
> Hail is open source. Use Hail Cloud, or host it yourself.

<!-- /copy -->

**`server.json`**. **Corrected 2026-07-07: using GitHub-org naming (Path B), and a shortened description (the original 222-char version fails real schema validation, 100-char hard limit).** The live file is [`mcp/server.json`](../../mcp/server.json). Edit it there and bump `version`; merging publishes it.

Asset source paths (already mirrored, confirmed resolving live at the URLs above): `hail-website/public/assets/brand/logo/monogram-512.png`, `hail-website/public/assets/brand/logo/monogram-1024.png`.

**Tool inventory** (for reference / any listing field that asks what the server does. Not a `server.json` field, the schema has no tags/tools array. This is the literal, code-verified list from `mcp/hailhq/mcp/tools.py`, 11 tools):

| Tool                   | Does                                                                           |
| ---------------------- | ------------------------------------------------------------------------------ |
| `place_call`           | Originate an outbound phone call.                                              |
| `send_email`           | Send an outbound email.                                                        |
| `get_call`             | Fetch the current state of one call.                                           |
| `list_calls`           | List recent calls, cursor-paginated.                                           |
| `get_email`            | Fetch one email's full record (body + inbound headers/verdicts).               |
| `list_emails`          | List emails; `direction="inbound"` for replies.                                |
| `get_email_raw`        | Presigned URL for an inbound email's raw MIME source.                          |
| `get_email_attachment` | Presigned URL for one inbound attachment.                                      |
| `get_email_events`     | Per-message delivery/engagement timeline (sent, delivered, opened, and so on). |
| `get_email_stats`      | Account-level deliverability stats (rates, time series).                       |
| `get_events`           | Page through the org- or call-level event stream.                              |

**Install/usage snippet** (what a reader clicks through after finding the listing):

```
Server URL: https://mcp.hail.so
Auth: OAuth (click Allow when your client prompts): cloud
      Bearer HAIL_API_KEY: self-host
```

## Notes

- **Namespace choice is a one-way door per name.** Once published under `so.hail/hail-mcp` (or `io.github.hail-hq/hail-mcp`), later switching auth methods means publishing under a _new_ name. The old listing does not migrate. Decide DNS vs. GitHub before running step 5, not after.
- **SMS is not yet an MCP tool.** `core/hailhq/core/providers/` currently wires up voice and email only; SMS is an approved-but-unimplemented spec (`docs/superpowers/specs/2026-07-06-sms-support-design.md`). The one-liner/description above name SMS as a Hail capability per the team's stated positioning call (core-capability claims are written present-tense regardless of today's milestone state, see `docs/superpowers/specs/2026-07-06-registry-submissions-design.md`), but the `server.json` tool inventory only lists what's actually callable today. Once SMS tools land in `mcp/hailhq/mcp/tools.py`, bump `version` and republish (`mcp-publisher publish` again with the same `name`, higher `version`).
- **Self-host isn't listed as a `packages` entry on purpose.** There's a public image at `ghcr.io/hail-hq/hail-mcp:latest` (GHCR is an allowlisted OCI host for this registry), but it's a mutable `:latest`/`sha-<commit>` deploy artifact, not a versioned release, so it does not map cleanly to the registry's per-version package model. This matches Hail's deliberate remote-only MCP distribution stance (see "Why remote-only" in `docs/setup/mcp.md`); the listing is the remote endpoint only.
- **Reachability check caveat:** `https://mcp.hail.so/` correctly 401s without a bearer token (OAuth-protected resource). That is expected and is what "publicly accessible" means here, not an open 200. Verified live 2026-07-06.
- **Registry is in preview** per its own docs. Expect possible schema/breaking changes; `mcp-publisher validate` will flag deprecated schema versions if the pinned `$schema` URL above drifts.
- Review turnaround: none. Publish is synchronous and self-service, no moderation queue observed in the docs.
- **2026-07-07 update:** `description` has a hard 100-char limit enforced by real server-side validation (`mcp-publisher validate` returns 422 `expected length <= 100`). Not documented anywhere we found ahead of time, only caught by actually running validation. Shortened from 222 to 86 chars. If this field is ever expanded, re-validate before publishing rather than trusting the length looks "about right."
- **2026-07-07 update:** confirmed `r13i` is a `hail-hq` org member (`gh api orgs/hail-hq/members/r13i` returns 204), so Path B (GitHub) was used instead of Path A (DNS). No DNS zone access was needed after all. `server.json` ready and validated at `mcp/server.json`. Only remaining step is `mcp-publisher login github` + `mcp-publisher publish`, which needs a human to complete the device-flow browser authorization.
