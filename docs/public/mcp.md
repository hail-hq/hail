# MCP clients

Hail exposes MCP as a **remote server**. Hail Cloud uses OAuth. Paste the URL into your client. Click Allow in the browser. Your agent then gets the call/sms/mail tools. There are no keys to manage and no installation.

> For the easy onboarding path, refer to the [client picker on hail.so/mcp](https://hail.so/mcp). It has copy-paste setup for the 8 most common clients (Claude.ai, ChatGPT, Cursor, Gemini, …). This page is the technical reference behind those snippets.

## URL

- **Hail Cloud**: `https://mcp.hail.so`
- **Self-hosted**: `http://<your-host>:8081` — refer to [Self-host](#self-host) below.

The Streamable HTTP transport serves the MCP root path. There is no `/mcp` suffix and no SSE.

> For web-based clients (Claude.ai, ChatGPT), the URL must be reachable from the client's servers — public DNS + TLS. If you want web clients to reach a self-hosted instance, tunnel it via cloudflared / tailscale funnel.

## Tools

Example. Ask your agent: "Find me a US number that can call and text." It calls `quote_numbers` with:

```json
{ "country_code": "US", "capabilities": ["voice", "sms"] }
```

The server exposes 31 tools. Schemas (args, validation, return shapes) are the source of truth — refer to [`mcp/hailhq/mcp/tools.py`](https://github.com/hail-hq/hail/blob/main/mcp/hailhq/mcp/tools.py).

Every tool sets the MCP hints `readOnlyHint`, `destructiveHint`, `idempotentHint` and `openWorldHint`, so clients can ask for approval before a write. `release_number`, `delete_agent` and `acquire_number` are marked destructive. Each one also needs a confirmation argument (`confirm_e164`, `confirm_name`, `confirm_total_cents`).

`place_call`, `send_sms` and `send_email` need `recipient_consent` set to the JSON boolean `true`. Strings such as `"true"` and numbers such as `1` are refused before any request is sent.

| Tool                      | Does                                                               |
| ------------------------- | ------------------------------------------------------------------ |
| `place_call`              | Originate an outbound phone call.                                  |
| `get_call`                | Fetch the current state of one call.                               |
| `list_calls`              | List recent calls (cursor-paginated).                              |
| `send_sms`                | Send an outbound SMS (recipient consent is required).              |
| `get_sms`                 | Fetch the current state of one SMS.                                |
| `list_sms`                | List recent SMS messages (cursor-paginated).                       |
| `send_email`              | Send an outbound email (supports `attachment_ids`).                |
| `upload_email_attachment` | Upload a file, get back a reusable id.                             |
| `get_email`               | Fetch one email's full record (body + inbound headers).            |
| `list_emails`             | List emails (`direction="inbound"` for replies).                   |
| `get_email_raw`           | Presigned URL for an inbound email's raw MIME.                     |
| `get_email_attachment`    | Presigned URL for one inbound attachment.                          |
| `get_email_events`        | Page through one email's event history.                            |
| `get_email_stats`         | Aggregate email counts for a time window.                          |
| `get_events`              | Page through the event stream.                                     |
| `list_email_domains`      | List sending identities + the default `from` address.              |
| `whoami`                  | Identify the human behind the session (for `reply_to`).            |
| `list_contacts`           | List the org's contacts (members + manual contacts).               |
| `lookup_contact`          | Find one contact by name, email, or phone fragment.                |
| `create_contact`          | Add a manual contact.                                              |
| `list_agents`             | List saved agents.                                                 |
| `get_agent`               | Fetch one saved agent.                                             |
| `create_agent`            | Save an agent that answers calls and texts.                        |
| `update_agent`            | Change an agent. `clear_fields` removes optional values.           |
| `delete_agent`            | Delete an agent. Destructive. Needs `confirm_name`.                |
| `route_number`            | Choose which agent answers a number.                               |
| `quote_numbers`           | Compare live carrier offers for a new number (read-only).          |
| `acquire_number`          | Buy a quoted number. Spends money. Needs `confirm_total_cents`.    |
| `list_numbers`            | List the org's numbers (cursor-paginated).                         |
| `get_number`              | Fetch one number.                                                  |
| `release_number`          | Release a number. Destructive and permanent. Needs `confirm_e164`. |

### Getting a number over MCP

1. `quote_numbers`: `{"country_code": "US", "capabilities": ["voice", "sms"]}`. Offers list `monthly_cents`, `setup_cents` and `quote_id`. Money is in cents. Quotes expire in 10 minutes.
2. `acquire_number`: copy `quote_id`, `country_code` and `number_type` from the offer. Set `confirm_total_cents` to `monthly_cents + setup_cents`. For an offer with `monthly_cents: 115` and `setup_cents: 50`:

   ```json
   {
     "quote_id": "8b1f0c2e-0000-4000-8000-000000000001",
     "country_code": "US",
     "number_type": "local",
     "confirm_total_cents": 165
   }
   ```

   The server checks the total. If it differs from the quote, the call fails with 409 and nothing is charged. The result is the number plus an `idempotency_key`. To retry after a timeout, pass that key as `idempotency_key`.

3. `route_number`: `{"number_id": "<id>", "voice_agent_id": "<agent-id>"}`.

> **Warning:** `acquire_number` buys a real number. It charges the setup fee and the first month now, then renews monthly. The account balance is the only spending limit. Keep the human approval prompt on for this tool.

An offer with `readiness: "verification_required"` cannot be bought until the organization finishes verification in the console.

## Claude.ai (web)

1. **Settings → Connectors → Add custom connector**
2. Server URL: `https://mcp.hail.so`
3. Save. Claude prompts you to authorize on first use. Click **Allow** in the browser tab that opens.

That is all. There is no API key field.

## ChatGPT (web)

Custom MCP connectors are behind Developer Mode on ChatGPT today:

1. **Settings → Connectors → Advanced → Developer mode**
2. **Create** → paste `https://mcp.hail.so`
3. Save. ChatGPT guides you through the OAuth consent on the first call.

## Other clients

The [client picker](https://hail.so/mcp) has setup snippets for Cursor, Gemini, Windsurf, Copilot, Zed, Raycast, and Claude Desktop. All follow the same shape: paste the URL, then click Allow.

## Authorized apps

Each cloud client that you connect appears as a row at [`hail.so/console/apps`](https://hail.so/signin?next=%2Fconsole%2Fapps). Revoke deletes the consent and every active access and refresh token for that client. The client's next tool call returns 401 and runs OAuth again from the start.

Access tokens last 30 days, and refresh tokens last 180 days. In practice, you authorize each client again approximately every six months. The consent screen opens, you click **Allow**, and the client continues.

## Self-host

Self-hosted deployments do not use OAuth. Set `HAIL_API_KEY` and send it as a bearer token:

```sh
curl -H "Authorization: Bearer ${HAIL_API_KEY}" http://localhost:8081/
```

The MCP service selects its auth mode from env at boot. For the full env-var contract, refer to the [MCP modes table in the operations runbook](./self-host/operations.md#mcp-modes) (`HAIL_AUTH_URL` for cloud, `HAIL_API_KEY` for self-host, mutually exclusive).

## Why remote-only (no stdio / no PyPI install)

We release one MCP distribution: a remote HTTP endpoint. It is bundled with every Hail deploy. We deliberately do **not** publish a stdio MCP server on PyPI. The reasons:

1. **Web UIs cannot run stdio servers.** Claude.ai's MCP Connectors and ChatGPT's Custom Connectors accept only remote URLs. They cannot start local processes from a browser.
2. **Every terminal client also accepts a remote URL.** Claude Code, Claude Desktop, Cursor, and Zed all connect to a URL. The URL flow works for all clients. Stdio works only for a subset.
3. **Stdio fragments distribution.** Two artifacts (PyPI stdio wrapper + HTTP service) cause two versions to keep in sync, two install paths, and two failure modes.
4. **Installation friction.** Stdio requires Python + pip/uv on the user's dev machine. Remote HTTP requires nothing. You only paste a URL.

If a restricted client ever needs stdio, we will release a thin stdio-to-HTTP proxy on PyPI (approximately 50 LOC).
