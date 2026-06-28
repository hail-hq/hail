# Architecture

Hail v1 is three Python services + a Go CLI, wrapped around LiveKit Cloud.

```
 AI agent                                     Hail                                LiveKit Cloud         PSTN
(caller)  ─────MCP URL──►  Hail MCP ─HTTP─►  Hail API  ◄────►  SIP+WebRTC  ◄────► Twilio ◄────► 📞
                          (HTTP :8081)     (FastAPI :8080)
                                                 │
                                                 └─dispatch──►  Hail voicebot  (LiveKit Agents worker)
                                                                     │
                                                                     ├─ VAD:   Silero
                                                                     ├─ STT:   Deepgram
                                                                     ├─ LLM:   fallback(OpenAI → Gemini → Anthropic)
                                                                     │        or caller-provided endpoint
                                                                     └─ TTS:   Cartesia (→ ElevenLabs fallback)
```

## Services

- **api** (`:8080`, FastAPI) — REST surface; accepts `POST /calls` etc. Source of truth for OpenAPI.
- **mcp** (`:8081`, Streamable HTTP; legacy SSE during transition) — MCP server wrapping the API; what agent clients (Claude.ai, ChatGPT, Claude Code, Cursor) connect to. See [MCP setup](./setup/mcp.md).
- **voicebot** (LiveKit Agents worker) — registers with LiveKit Cloud; dispatched into a room per call.
- **postgres** — call records, phone numbers, API keys.
- **minio** (dev only) — S3-compatible local object storage. Swap for real S3 in prod.

LiveKit Cloud is external. The `hail` Go CLI is a human-facing scriptable tool, not a service.

## Outbound call flow

1. Caller (agent via MCP, or CLI, or direct HTTP) → `POST /calls` with `{to, from, first_message?, …llm}`.
2. Hail API creates a LiveKit room and dispatches the voicebot into it.
3. Voicebot joins; LiveKit places a SIP outbound via the Twilio trunk to `to`.
4. On pickup, voicebot speaks `first_message` (if set), then runs the STT → LLM → TTS loop.
5. On hangup, voicebot writes the call record to Postgres and uploads the recording to S3.

## LLM modes

**A — system prompt (default).** Caller supplies `system_prompt`. Voicebot uses LiveKit's `FallbackAdapter` chaining `openai.LLM` → `google.LLM` → `anthropic.LLM` (fast models each). Falls through on error.

**B — BYO endpoint.** Caller supplies `llm: { base_url, api_key, model }`. Voicebot points `openai.LLM` at that endpoint. No fallback.

One mode per call.

## Data

- **Postgres** — call records, phone numbers, API keys.
- **S3** — call recordings.
- **LiveKit Cloud** — transient media (ephemeral).

## Outbound email

Outbound mail goes through AWS SES via the `EmailProvider` adapter in `core/hailhq/core/providers/email/`. Two flavors of sender identity, stored in `email_domains`:

- **`kind='custom'`** — tenant-controlled DNS (e.g. `acme.com`). `POST /email-domains` registers the identity with SES and auto-configures a custom MAIL FROM on `send.<domain>`; the response surfaces three DKIM CNAMEs **plus** the MAIL FROM MX/SPF records (each carrying an optional `priority`). The tenant publishes them, then `POST /email-domains/{id}/verify` re-polls SES for both DKIM and MAIL FROM status. Verified custom domains can also **receive** — inbound matches by identity, one row + webhook per matched domain.
- **`kind='hail_mail'`** — per-org address under an operator-managed parent domain. The full sender is `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` (e.g. `alice+acme@mail.hail.so`); the parent domain is pre-verified once by the operator out of band, so per-org rows land already-verified without ever calling SES.

### Self-hosted vs managed

The two surfaces differ in where the prefixes come from and where they're edited:

|                       | Self-hosted Hail                                                                               | Managed Hail (hail.so)                                                                     |
| --------------------- | ---------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Auth                  | Shared `HAIL_API_KEY`; one sentinel "Self-hosted" org                                          | Per-user `hl_live_*` keys via the website's auth backend                                   |
| Org concept           | None — single sentinel org                                                                     | Real orgs with members                                                                     |
| Hail-mail base domain | `HAIL_MAIL_BASE_DOMAIN` (operator's `.env`)                                                    | `HAIL_MAIL_BASE_DOMAIN` (operator's deploy env)                                            |
| Hail-mail prefixes    | `HAIL_MAIL_DEFAULT_USER_PREFIX` + `HAIL_MAIL_DEFAULT_ORG_PREFIX` (`.env` is the configuration) | Same env vars provide the deploy-time default; org admins override per-org via the console |
| Where edits land      | Restart with new `.env` values                                                                 | `PATCH /email-domains/{id}` (console writes this)                                          |
| SES production access | Operator's AWS account                                                                         | Operator's AWS account                                                                     |
| Billing               | Off — `usage_events` accumulates as raw analytics                                              | Cloud rater applies cents/unit, debits `account_credits`                                   |

Both prefixes are validated against `^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$` (1–20 chars, lowercase alphanumeric + hyphen, no leading/trailing hyphen), so the full local part fits well under the RFC-5321 64-char budget.

### Send-time resolution

`POST /emails` picks a sender in this order:

1. Explicit `from` — must match a `verified` row owned by the caller's org.
2. First verified org-owned domain (ordered by `created_at`, so a tenant's "default sender" stays stable).
3. Auto-mint a hail-mail row using the configured prefixes, if `HAIL_MAIL_BASE_DOMAIN` is set.

If none of those resolve, the request returns `503` pointing at how to register a domain.

See [`docs/setup/aws-ses.md`](./setup/aws-ses.md) for the operator-side setup, and [`docs/superpowers/plans/2026-05-17-hail-mail-addressing.md`](./superpowers/plans/2026-05-17-hail-mail-addressing.md) for the addressing/configurability plan.

## Inbound email

Operators on AWS enable inbound by applying `infra/terraform/`, which
provisions an S3 bucket, an SES Receipt Rule + Rule Set, and a small
Lambda that signs and forwards SES events into Hail's
`POST /internal/ses-events` endpoint. The API parses raw MIME from S3,
routes the message to the owning org by parsing the hail-mail
local-part (`<user>+<org>@mail.hail.so`), persists an `Email` row with
`direction='inbound'`, and fans out events to per-domain webhooks and
org-wide subscriptions via the background delivery worker.

```
inbound SMTP ──► SES Receipt Rule
                 ├─ Action: S3       → s3://hail-inbound/raw/<msgid>
                 └─ Action: Lambda   → POST /internal/ses-events (HMAC-signed)
                                          │
                                          ▼
                                    Hail API:
                                      • verify HMAC
                                      • fetch raw MIME from S3
                                      • parse MIME, route to org
                                      • write Email row (direction='inbound')
                                      • enqueue email_attachments to S3
                                      • fan out webhook deliveries
                                      • enqueue forwarding sends (header rewrite)
```

The cloud-agnostic SMTP path is stubbed
([`SmtpInboundProvider`](../core/hailhq/core/providers/email/inbound/smtp.py))
and tracked in [`docs/setup/smtp-inbound.md`](setup/smtp-inbound.md).

### Per-domain routing — forward and/or webhook

Each `email_domains` row carries `inbound_enabled`, `forward_to`, and `webhook_url`. When `inbound_enabled` is true:

- `forward_to` (list of addresses) triggers one outbound `Email` row per target through the existing send loop, with header rewrite (envelope `From:` = `forwarder+<org>@mail.hail.so`, `Reply-To:` = original sender, `References:` preserved for threading, `X-Hail-Forward-Hops` and `Auto-Submitted: auto-forwarded` for loop suppression).
- `webhook_url` triggers a signed POST through the webhook delivery worker.

A separate `inbound_routes` table for per-mailbox routing within custom domains is deferred to a future milestone (when tenants point their own MX at SES).

### Org-wide subscriptions

The `/webhooks` CRUD surface is the firehose pattern — one subscription covers multiple event types (`email.received`, `email.bounced`, `email.complained`). Stripe-style signing: `X-Hail-Signature: t=<unix>,v1=<hex>`. Retries on a fixed `0/30s/2m/10m/1h/6h/24h` ladder; after the last retry the delivery is marked `dead` and after 50 consecutive dead deliveries the subscription auto-disables.

See [`docs/setup/aws-ses.md`](./setup/aws-ses.md) §10 for the operator runbook and [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](./superpowers/specs/2026-06-06-inbound-email-design.md) for the full spec.
