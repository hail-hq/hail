# MCP clients

Hail exposes MCP as a **remote server**. Cloud is OAuth: paste the URL into your client, click Allow in the browser, your agent gets call/sms/mail tools. No keys to manage, no install.

> Looking for the easy onboarding path? The [client picker on hail.so/mcp](https://hail.so/mcp) has copy-paste setup for the 8 most common clients (Claude.ai, ChatGPT, Cursor, Gemini, …). This page is the technical reference behind those snippets.

## URL

- **Hail Cloud**: `https://mcp.hail.so`
- **Self-hosted**: `http://<your-host>:8081` — see [Self-host](#self-host) below.

The Streamable HTTP transport serves the MCP root path; no `/mcp` suffix, no SSE.

> For web-based clients (Claude.ai, ChatGPT) the URL must be reachable from the client's servers — public DNS + TLS. Tunnel a self-hosted instance via cloudflared / tailscale funnel if you want it reachable to web clients.

## Tools

The server exposes ten tools. Schemas (args, validation, return shapes) are the source of truth — see [`mcp/hailhq/mcp/tools.py`](../../mcp/hailhq/mcp/tools.py).

| Tool                      | Does                                                    |
| ------------------------- | ------------------------------------------------------- |
| `place_call`              | Originate an outbound phone call.                       |
| `send_email`              | Send an outbound email (supports `attachment_ids`).     |
| `upload_email_attachment` | Upload a file, get back a reusable id.                  |
| `get_call`                | Fetch the current state of one call.                    |
| `list_calls`              | List recent calls (cursor-paginated).                   |
| `get_email`               | Fetch one email's full record (body + inbound headers). |
| `list_emails`             | List emails (`direction="inbound"` for replies).        |
| `get_email_raw`           | Presigned URL for an inbound email's raw MIME.          |
| `get_email_attachment`    | Presigned URL for one inbound attachment.               |
| `get_events`              | Page through the event stream.                          |

## Claude.ai (web)

1. **Settings → Connectors → Add custom connector**
2. Server URL: `https://mcp.hail.so`
3. Save. Claude prompts you to authorize on first use; click **Allow** in the browser tab that opens.

That's it. No API key field.

## ChatGPT (web)

Custom MCP connectors live behind Developer Mode on ChatGPT today:

1. **Settings → Connectors → Advanced → Developer mode**
2. **Create** → paste `https://mcp.hail.so`
3. Save. ChatGPT walks you through the OAuth consent on first call.

## Other clients

The [client picker](https://hail.so/mcp) has setup snippets for Cursor, Gemini, Windsurf, Copilot, Zed, Raycast, and Claude Desktop. All follow the same shape: paste URL, click Allow.

## Authorized apps

Each cloud client you connect appears as a row at [`hail.so/console/apps`](https://hail.so/console/apps). Revoke deletes the consent + every active access and refresh token for that client; the client's next tool call returns 401 and re-runs OAuth from scratch.

Access tokens last 30 days, refresh tokens 180. In practice you'll re-authorize each client roughly every six months — the consent screen pops, you click **Allow**, and the client resumes.

## Self-host

Self-hosters skip OAuth — set `HAIL_API_KEY` and send it as a bearer:

```sh
curl -H "Authorization: Bearer ${HAIL_API_KEY}" http://localhost:8081/
```

The MCP service picks its auth mode from env at boot — see the [MCP modes table in the operations runbook](../operations.md#mcp-modes) for the full env-var contract (`HAIL_AUTH_URL` for cloud, `HAIL_API_KEY` for self-host, mutually exclusive).

## Why remote-only (no stdio / no PyPI install)

We ship one MCP distribution — a remote HTTP endpoint, bundled with every Hail deploy. We deliberately do **not** publish a stdio MCP server on PyPI. Reasons:

1. **Web UIs can't run stdio servers.** Claude.ai's MCP Connectors and ChatGPT's Custom Connectors only accept remote URLs — they can't spawn local processes from a browser.
2. **Every terminal client also accepts a remote URL.** Claude Code, Claude Desktop, Cursor, Zed all connect to a URL. The URL flow works universally; stdio works only for a subset.
3. **Stdio fragments distribution.** Two artifacts (PyPI stdio wrapper + HTTP service) mean two versions to keep in sync, two install paths, two failure modes.
4. **Install friction.** Stdio requires Python + pip/uv on the user's dev machine. Remote HTTP requires nothing — paste a URL.

If a restricted client ever needs stdio, we'll ship a thin stdio-to-HTTP proxy on PyPI (≈50 LOC).
