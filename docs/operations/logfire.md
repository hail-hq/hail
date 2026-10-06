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
FastAPI/Starlette requests, HTTPX/Requests/aiohttp clients, SQLAlchemy operations,
MCP requests, native LiveKit voice/model/tool spans, SMS agent invocations,
webhook deliveries, and email forwards. Request spans carry a generated
`request_id`; authenticated API requests carry `organization_name`, `organization_id`, `user_email`, `user_id`, and `auth_kind`. Other API workers share the API's log,
HTTP, and database instrumentation. The API injects W3C trace context into
outbound LiveKit dispatch metadata. SMS work is correlated by SMS, agent and
organization IDs; it starts a new trace when claimed from the database.

The common export boundary removes conversation content, tool arguments/results,
MCP payloads, SQL text, URL credentials/query strings/fragments, and GenAI content
events. Authenticated actor identity fields intentionally retain the acting user's
email and organization name; these fields propagate into child spans and logs.
Automated inbound voice/SMS agents carry organization identity and `actor_kind=agent`,
without inventing a human actor. Other logs and exceptions redact email addresses, international
phone numbers and credential patterns. Free-form application logs must still
avoid sensitive data; regex redaction cannot identify arbitrary personal prose.
Console logs and database audit records retain their existing behavior.

Health probes and duplicate Uvicorn access logs are excluded. Empty SMS claims
are suppressed; other worker polling SQL remains visible. Sampling applies to
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
