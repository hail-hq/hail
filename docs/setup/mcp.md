# MCP clients

Hail exposes MCP as a **remote server**. The current transport is **Streamable HTTP** at the server root; the legacy **SSE** endpoint at `/sse` stays mounted during a transition window. Agents connect by URL; no local install.

> Looking for the easy onboarding path? The [client picker on hail.so/mcp](https://hail.so/mcp) has copy-paste setup snippets for the 8 most common clients (Claude.ai, ChatGPT, Cursor, Gemini, …). This page is the technical reference behind those snippets.

## URL

- **Self-hosted**: `http://<your-host>:8081` (Streamable HTTP at the root) — `/sse` still works for older clients.
- **Hail Cloud** (later): `https://mcp.hail.so`

Authenticate with your `HAIL_API_KEY` as a bearer token.

> For web-based clients (Claude.ai, ChatGPT), the URL must be reachable from the client's servers — public DNS + TLS. Expose via a tunnel (cloudflared, tailscale funnel) or reverse proxy. For localhost use, stick to terminal clients.

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
2. URL: `https://<your-host>`
3. Authentication: Bearer token = `HAIL_API_KEY`
4. Save

## ChatGPT (web)

1. **Custom Connectors → Create**
2. Paste the URL + API key
3. Save

## Claude Code / Cursor

These read a remote entry straight from their MCP config file (`.mcp.json` for Claude Code, `~/.cursor/mcp.json` for Cursor):

```json
{
  "mcpServers": {
    "hail": {
      "type": "http",
      "url": "http://localhost:8081",
      "headers": {
        "Authorization": "Bearer ${HAIL_API_KEY}"
      }
    }
  }
}
```

Claude Code can also add it from the CLI:

```sh
claude mcp add --transport http hail http://localhost:8081 \
  --header "Authorization: Bearer ${HAIL_API_KEY}"
```

Cursor uses the same block without the `type` field — just `url` + `headers`.

## Claude Desktop

Desktop's `claude_desktop_config.json` loads **local (stdio) servers only** — a bare remote `url` entry is rejected as invalid. Two ways to connect a remote server:

- **Connectors UI** (no config edit): Settings → Connectors → Add custom connector → paste the server URL, with `HAIL_API_KEY` as the bearer token.
- **Bridge it in the config file** with [`mcp-remote`](https://www.npmjs.com/package/mcp-remote) (needs Node), which proxies the remote MCP endpoint as a local stdio server:

```json
{
  "mcpServers": {
    "hail": {
      "command": "npx",
      "args": [
        "-y",
        "mcp-remote",
        "http://localhost:8081",
        "--header",
        "Authorization: Bearer ${HAIL_API_KEY}"
      ],
      "env": { "HAIL_API_KEY": "hl_live_…" }
    }
  }
}
```

## Why no stdio / no PyPI install

We ship one MCP distribution — a remote HTTP endpoint, bundled with every Hail deploy. We deliberately do **not** publish a stdio MCP server on PyPI. Reasons:

1. **Web UIs can't run stdio servers.** Claude.ai's MCP Connectors and ChatGPT's Custom Connectors only accept remote URLs — they can't spawn local processes from a browser. A PyPI stdio package would serve none of those users.
2. **Every terminal client also accepts a remote URL.** Claude Code, Claude Desktop, and Cursor all connect to a remote URL. The URL flow works universally; stdio works only for a subset.
3. **Stdio fragments distribution.** Two artifacts (PyPI stdio wrapper + HTTP service) mean two versions to keep in sync, two install paths to document, two failure modes.
4. **Install friction.** Stdio requires Python + pip/uv on the user's dev machine. Remote HTTP requires nothing — paste a URL.

If a real user later needs stdio (e.g. a restricted client that doesn't do remote HTTP), we'll ship a thin stdio-to-HTTP proxy on PyPI. Roughly 50 lines of code, trivial to add when a concrete need exists.
