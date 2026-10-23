# Backend telemetry (Logfire EU)

Enable in the deployment's secret `.env` file, then roll the services through the
normal deployment workflow:

```dotenv
HAIL_LOGFIRE_ENABLED=true
LOGFIRE_TOKEN=<your-project-send-only-token>
LOGFIRE_ENVIRONMENT=production
LOGFIRE_BASE_URL=https://logfire-eu.pydantic.dev
LOGFIRE_SERVICE_VERSION=<release SHA>
HAIL_LOGFIRE_SAMPLE_RATE=1.0
```

For local development, select the same project with the CLI and point
`LOGFIRE_CREDENTIALS_DIR` at its absolute `.logfire` directory. Set
`LOGFIRE_ENVIRONMENT=development`. Never commit credentials or bake them into
images. Compose already passes `.env` to all three services.

Open your project in Logfire. Add `organization_name`, `user_email`, `actor_kind`,
and the corresponding IDs as Live/Explore columns to scan who did what.
Filter by `service_name`: `hail-api`, `hail-voicebot`, or `hail-mcp`.

Coverage: existing Hail INFO+ logs and dependency WARNING+ logs, exceptions,
FastAPI/Starlette requests, HTTPX/Requests/aiohttp clients,
MCP requests, native LiveKit voice/model/tool spans, SMS agent invocations,
webhook deliveries, and email forwards. Request spans carry a generated
`request_id`; authenticated API requests carry `organization_name`, `organization_id`, `user_email`, `user_id`, and `auth_kind`. Other API workers share the API's log,
HTTP, and database instrumentation. The API injects W3C trace context into
outbound LiveKit dispatch metadata. SMS work is correlated by SMS, agent and
organization IDs; it starts a new trace when claimed from the database.

## Trace tags

Every span and log of a call, SMS, email or MCP tool call carries these
attributes. Child spans (Hail and native LiveKit) and logs get them from the
scope, set with `tag_context()` in
[`telemetry_identity.py`](../../core/hailhq/core/telemetry_identity.py).

| Attribute | Values |
| --- | --- |
| `hail.channel` | `voice`, `sms`, `email`, `mcp`, `api` |
| `hail.direction` | `inbound`, `outbound` |
| `hail.call_id`, `hail.sms_id`, `hail.email_id`, `hail.agent_id` | record ids |
| `hail.from`, `hail.to` | the two parties |
| `mcp.tool.name` | the MCP tool called |
| `organization_id`, `organization_name` | the organization |

The Hail side (`hail.from` on outbound, `hail.to` on inbound) is the full
number or address. The other party is masked: a phone keeps its country code
and last 4 digits (`+46 ***4567`), an email keeps its domain
(`***@example.com`). The trace's root span also gets Logfire tags, for
example `voice` and `outbound`, shown in the Live view.

```sql
SELECT start_timestamp, span_name, attributes->>'hail.to' AS to_party
FROM records
WHERE attributes->>'hail.channel' = 'voice'
  AND attributes->>'hail.direction' = 'inbound'
ORDER BY start_timestamp DESC LIMIT 20
```

## LLM content

The export always sends these keys in full, not masked:
`gen_ai.system_instructions`, `gen_ai.input.messages`,
`gen_ai.output.messages`, `gen_ai.tool.call.arguments`,
`gen_ai.tool.call.result` and `lk.pii.user_transcript`. Logfire shows the
`llm_request` spans of a voice call or SMS reply in its LLM view: the system
prompt, the conversation and the reply. Tool definitions stay dropped.
This content can hold the other party's full number, address or name:
masking applies only to the `hail.from` and `hail.to` tags.

There is no attribute length limit: the OpenTelemetry SDK default is
unlimited and Hail does not set `OTEL_ATTRIBUTE_VALUE_LENGTH_LIMIT`, so
long prompts are not cut. LiveKit sends the content unless the LiveKit Cloud
project turns on redaction (LiveKit Cloud dashboard), which strips it for
every exporter.

## Export filter

Apart from the LLM content keys above, the common export boundary removes
conversation content, tool arguments/results, MCP payloads, SQL text, URL
credentials/query strings/fragments, and GenAI content events. Authenticated actor identity fields intentionally retain the acting user's
email and organization name; these fields propagate into child spans and logs.
Automated inbound voice/SMS agents carry organization identity and `actor_kind=agent`,
without inventing a human actor. Other logs and exceptions redact email addresses, international
phone numbers and credential patterns. Free-form application logs must still
avoid sensitive data; regex redaction cannot identify arbitrary personal prose.
Console logs and database audit records retain their existing behavior.

Health probes and duplicate Uvicorn access logs are excluded. On `hail-mcp`,
the discovery spans (`ping`, `tools/list`, `prompts/list`, `resources/list`,
`resources/templates/list`), `GET /` (the event stream), `/.well-known/*`,
`POST /` envelopes and `DELETE /` session closes (kept when the status is
400 or more), and 404s on unknown paths (scanners) are not exported;
`tools/call` is. Empty SMS claims
are suppressed. SQL query spans are off: the SQLAlchemy instrumentation does
not support SQLAlchemy 2.1 yet, and the API logs "SQLAlchemy tracing is off"
at startup. They return once a release supports it. Sampling applies to
traces and logs within them; standalone logs are retained. At 1.0 nothing is
sampled out. All HTTP export uses the EU endpoint, with five-second network
and flush timeouts. Shutdown flushes pending telemetry. Existing LiveKit Cloud exporters are
preserved; only the copy sent to Logfire is filtered.

Verification query (CLI OAuth login required):

```sh
uvx logfire-cli --region=eu --org YOUR_ORGANIZATION --no-input --output json mcp query run \
  "SELECT start_timestamp, service_name, span_name FROM records WHERE start_timestamp > now() - interval '15 minutes' ORDER BY start_timestamp DESC LIMIT 30" \
  --project YOUR_PROJECT
```

Caddy/container/host telemetry, SES Lambda instrumentation, production alerts
and dashboards require a separate infrastructure rollout. Production deployments
the deploy workflow writes `LOGFIRE_SERVICE_VERSION` into the VM `.env` as the deployed commit SHA for API, MCP, and voicebot.
CLI and SDK package versions are unaffected. This change does not
deploy or restart production services.
