---
target: "mcp.so"
slug: mcp-so
category: mcp-registry
url: "https://mcp.so/submit"
score: 6.5
status: drafted
---

# mcp.so

## TODO

- [x] Checked 2026-11-16: Hail is not listed (search "hail" on mcp.so; no GitHub issue for hail-hq in chatmcp/mcpso)
- [ ] Submitted as a GitHub issue (free route)
- [ ] Confirmed live: record the listing URL in **Notes**

## Steps to submit

The web form at <https://mcp.so/submit> now charges a one-time $39 fee ("Publish immediately without review"). The free route is a GitHub issue in `chatmcp/mcpso`. Other remote servers use the title "Submit Remote MCP Server: <Name> (<registry name>)".

1. Save the issue body from **Content** below to a file, for example `mcpso-issue.md`.
2. Open the issue from the folder that holds that file, signed in to the `gh` CLI:

```bash
gh issue create \
  --repo chatmcp/mcpso \
  --title "Submit Remote MCP Server: Hail (io.github.hail-hq/hail-mcp)" \
  --body-file mcpso-issue.md
```

3. Record the issue URL in **Notes**. Check <https://mcp.so/search?q=hail> for the listing later.

## Content

Issue body:

````markdown
### Server Name
Hail

### Category
Communication

### Official Website / Documentation
https://hail.so/docs/mcp

### Repository
https://github.com/hail-hq/hail (open source, AGPLv3). The MCP server is in the `mcp/` folder. The hosted server needs no local install.

### Remote MCP Endpoint
`https://mcp.hail.so`, Streamable HTTP. OAuth 2.1 with dynamic client registration; sign in with a Hail account.

### Remote MCP Config
```json
{
  "mcpServers": {
    "hail": {
      "type": "http",
      "url": "https://mcp.hail.so"
    }
  }
}
```

### Description
Hail gives an AI agent a phone number, an inbox and a voice. One remote MCP server covers phone calls, text messages and email.

Your agent can:
- Place an outbound AI call and read its transcript
- Send and receive text messages
- Send email, read replies and attachments, and check delivery events
- Buy phone numbers in 120+ countries and route them to a saved agent
- Submit carrier registrations, such as US A2P 10DLC

Hail can also be self-hosted. The same features are in the CLI, the Python SDK and the OpenAPI spec.

Listed in the official MCP registry as `io.github.hail-hq/hail-mcp`.
````

## Notes

- 2026-11-16: the old two-field form (name, GitHub URL) from 2026-07 is gone. The page now has tabs (MCP Server, Remote Server, MCP Client, AI Product) and the $39 paid option.
- No review time is stated. The repo had about 4,500 open issues on 2026-11-16.
