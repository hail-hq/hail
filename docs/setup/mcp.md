# MCP setup

Hail exposes MCP as a **remote server over SSE**. Agents connect by URL; no local install.

## URL

- **Self-hosted**: `http://<your-host>:8081/sse` (from `docker compose up`)
- **Hail Cloud** (later): `https://mcp.hail.so/sse`

Authenticate with your `HAIL_API_KEY` as a bearer token.

> For web-based clients (Claude.ai, ChatGPT), the URL must be reachable from the client's servers — public DNS + TLS. Expose via a tunnel (cloudflared, tailscale funnel) or reverse proxy. For localhost use, stick to terminal clients.

## Claude.ai (web)

1. **Settings → Connectors → Add custom connector**
2. URL: `https://<your-host>/sse`
3. Authentication: Bearer token = `HAIL_API_KEY`
4. Save

## ChatGPT (web)

1. **Custom Connectors → Create**
2. Paste the URL + API key
3. Save

## Claude Code / Claude Desktop / Cursor

Add an SSE entry to your MCP config (e.g. `.mcp.json` or `claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "hail": {
      "type": "sse",
      "url": "http://localhost:8081/sse",
      "headers": {
        "Authorization": "Bearer ${HAIL_API_KEY}"
      }
    }
  }
}
```

## Why no stdio / no PyPI install

We ship one MCP distribution — a remote SSE endpoint, bundled with every Hail deploy. We deliberately do **not** publish a stdio MCP server on PyPI. Reasons:

1. **Web UIs can't run stdio servers.** Claude.ai's MCP Connectors and ChatGPT's Custom Connectors only accept remote URLs — they can't spawn local processes from a browser. A PyPI stdio package would serve none of those users.
2. **Every terminal client also accepts SSE.** Claude Code, Claude Desktop, and Cursor all support SSE transports. The URL flow works universally; stdio works only for a subset.
3. **Stdio fragments distribution.** Two artifacts (PyPI stdio wrapper + SSE service) mean two versions to keep in sync, two install paths to document, two failure modes.
4. **Install friction.** Stdio requires Python + pip/uv on the user's dev machine. Remote SSE requires nothing — paste a URL.

If a real user later needs stdio (e.g. a restricted client that doesn't do SSE), we'll ship a thin stdio-to-SSE proxy on PyPI. Roughly 50 lines of code, trivial to add when a concrete need exists.
