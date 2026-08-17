# Architecture

Hail v1 is three Python services plus a Go CLI, built around LiveKit Cloud.

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

- **api** (`:8080`, FastAPI) — the REST surface; it accepts `POST /calls` and the other routes. It is the source of truth for OpenAPI.
- **mcp** (`:8081`, Streamable HTTP; legacy SSE during the transition) — the MCP server that wraps the API. Agent clients (Claude.ai, ChatGPT, Claude Code, Cursor) connect to it. Refer to [MCP setup](./mcp.md).
- **voicebot** (LiveKit Agents worker) — registers with LiveKit Cloud. Hail dispatches it into a room for each call.
- **postgres** — call records, phone numbers, API keys.
- **minio** (dev only) — S3-compatible local object storage. Use real S3 in production.

LiveKit Cloud is external. The `hail` Go CLI is a scriptable tool for humans, not a service.

## Outbound call flow

1. The caller (an agent via MCP, the CLI, or direct HTTP) sends `POST /calls` with `{to, from, first_message?, …llm}`.
2. The Hail API creates a LiveKit room and dispatches the voicebot into it.
3. The voicebot joins the room. LiveKit places an outbound SIP call through the Twilio trunk to `to`.
4. On pickup, the voicebot classifies who answered before it speaks (refer to the section below). Then it speaks the AI disclosure and the `first_message` (if set), and runs the STT → LLM → TTS loop. If the call set `ai_disclosure: false`, the voicebot skips the disclosure; Hail audit-logs the opt-out, and the opt-out is the caller's responsibility.
5. On hangup, the voicebot writes the call record to Postgres and uploads the recording to S3.

### Answering machine detection and DTMF

Every outbound call runs LiveKit's AMD ([`voicebot/hailhq/voicebot/amd.py`](https://github.com/hail-hq/hail/blob/main/voicebot/hailhq/voicebot/amd.py)) against the greeting. Classification runs on the session's own LLM and STT, not on LiveKit Inference. On `machine-vm` and `machine-unavailable`, the voicebot hangs up without a word — it never leaves a message. It records `status=no_answer` with `end_reason=voicemail_reached` / `machine_unavailable`. On `human` and `uncertain`, the call proceeds normally.

On `machine-ivr`, the voicebot does **not** speak the greeting. A menu cannot hear it, and `session.say` is TTS-only, so the LLM would get no turn in which to press a key. Instead, the agent takes a real LLM turn (`generate_reply`) with the captured menu text and the `send_dtmf` tool. Hail defers the disclosure until the first person speaks. Hail writes the verdict to `call_events` as an `amd_result` row on every call. A detection failure is non-fatal: the call proceeds as if a human answered.

Billing no longer requires a `completed` status. Hail bills a call when `answered_at` is set (the SIP leg went active) **or** when the call completed normally. The first clause bills a machine-answered call and a call that failed mid-conversation. The second clause keeps billing for a completed call whose answer signal never arrived.

The agent can press keypad digits at any point with the `send_dtmf` tool ([`core/hailhq/core/agent_tools/send_dtmf.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/agent_tools/send_dtmf.py)) — not only after an IVR verdict. Thus the agent can navigate phone trees that it reaches mid-call.

## LLM modes

**A — system prompt (default).** The caller supplies `system_prompt`. The voicebot uses LiveKit's `FallbackAdapter`, which chains `openai.LLM` → `google.LLM` → `anthropic.LLM` (a fast model for each). It falls through on error.

**B — BYO endpoint.** The caller supplies `llm: { base_url, api_key, model }`. The voicebot points `openai.LLM` at that endpoint. There is no fallback.

**C — standing BYO endpoint.** The organization saves an endpoint once on the console Providers page. Every call uses it, with opt-in fallback to Hail's models. A per-call mode B block overrides it.

Precedence is B, then C, then A. See [Bring your own LLM](./byo-llm.md) for the wire contract and a runnable endpoint.

## Data

- **Postgres** — call, SMS, and email records; phone numbers; email domains; contacts; webhook subscriptions; API keys.
- **S3** — call recordings.
- **LiveKit Cloud** — transient media (ephemeral).

## SMS

The `SmsProvider` adapter in [`core/hailhq/core/providers/sms/`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/providers/sms) sends SMS through Twilio.

**Outbound.** `POST /sms` sends from the org's dedicated SMS-capable number. There is no pool fallback — refer to `hail numbers` for number acquisition. Twilio posts delivery-status callbacks. These callbacks move `Sms.status` and fan out the `sms.delivered` / `sms.undelivered` / `sms.failed` webhook events.

**Inbound.** Twilio posts each incoming message to `POST /sms/inbound`. Hail verifies the `X-Twilio-Signature` header. It matches the destination number to an org, stores the message, and fires the `sms.received` webhook event. Messages to unknown or pool numbers are dropped. An opt-out reply (`STOP`) adds the sender to the org's suppression list; `START` removes it.

## Outbound email

The `EmailProvider` adapter in `core/hailhq/core/providers/email/` sends outbound mail through AWS SES. Hail stores two kinds of sender identity in `email_domains`:

- **`kind='custom'`** — tenant-controlled DNS (for example `acme.com`). `POST /email-domains` registers the identity with SES and auto-configures a custom MAIL FROM on `send.<domain>`. The response surfaces three DKIM CNAMEs **plus** the MAIL FROM MX/SPF records (each with an optional `priority`). The tenant publishes them, then calls `POST /email-domains/{id}/verify` to re-poll SES for the DKIM and MAIL FROM status. Verified custom domains can also **receive** — inbound matches by identity, with one row and one webhook per matched domain.
- **`kind='hail_mail'`** — a per-org address under an operator-managed parent domain. The full sender is `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` (for example `alice+acme@mail.hail.so`). The operator pre-verifies the parent domain once, out of band. Thus Hail creates per-org rows as already verified, without a call to SES.

### Self-hosted vs managed

The two surfaces differ in the source of the prefixes and in the place where you edit them:

|                       | Self-hosted Hail                                                                                                         | Managed Hail (hail.so)                                                                              |
| --------------------- | ------------------------------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------- |
| Auth                  | Shared `HAIL_API_KEY`; one sentinel "Self-hosted" org                                                                    | Per-user `hl_live_*` keys via the website's auth backend                                            |
| Org concept           | None — single sentinel org                                                                                               | Real orgs with members                                                                              |
| Hail-mail base domain | `HAIL_MAIL_BASE_DOMAIN` (operator's `.env`)                                                                              | `HAIL_MAIL_BASE_DOMAIN` (operator's deploy env)                                                     |
| Hail-mail prefixes    | User prefix from `HAIL_MAIL_FROM` / `HAIL_MAIL_DEFAULT_USER_PREFIX` (`.env`); org prefix derived per-org from the org id | Same: user prefix from env, org prefix derived per-org; org admins override per-org via the console |
| Where edits land      | Restart with new `.env` values                                                                                           | `PATCH /email-domains/{id}` (console writes this)                                                   |
| SES production access | Operator's AWS account                                                                                                   | Operator's AWS account                                                                              |
| Billing               | Off — `usage_events` accumulates as raw analytics                                                                        | Cloud rater applies cents/unit, debits `account_credits`                                            |

Hail validates both prefixes against `^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$` (1–20 chars, lowercase alphanumeric + hyphen, no leading or trailing hyphen). Thus the full local part stays well under the RFC-5321 64-char budget.

### Send-time resolution

`POST /emails` picks a sender in this order:

1. An explicit `from` — it must match a `verified` row that the caller's org owns.
2. The org's single verified domain. With two or more, there is no default: the request returns `422` listing them, and the caller must pass `from`.
3. An auto-minted hail-mail row with the configured prefixes, if `HAIL_MAIL_BASE_DOMAIN` is set.

If none of those resolve, the request returns `503` with instructions on how to register a domain.

`GET /email-domains` answers the same question ahead of a send: its `default_from` field holds the address a `from`-less send would use, and is `null` when the caller must choose.

Refer to [AWS SES setup](./self-host/aws-ses.md) for the operator-side setup. Refer to [`docs/superpowers/plans/2026-05-17-hail-mail-addressing.md`](https://github.com/hail-hq/hail/blob/main/docs/superpowers/plans/2026-05-17-hail-mail-addressing.md) for the addressing/configurability plan.

## Inbound email

Operators on AWS enable inbound email when they apply `infra/terraform/`.
The Terraform provisions an S3 bucket, an SES Receipt Rule + Rule Set,
and a small Lambda. The Lambda signs SES events and forwards them to
Hail's `POST /internal/ses-events` endpoint. The API parses the raw MIME
from S3 and routes the message to the owning org by the hail-mail
local-part (`<user>+<org>@mail.hail.so`). It persists an `Email` row
with `direction='inbound'`. The background delivery worker fans out
events to per-domain webhooks and org-wide subscriptions.

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

The cloud-agnostic SMTP path is a stub
([`SmtpInboundProvider`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/providers/email/inbound/smtp.py)).
[SMTP inbound](./self-host/smtp-inbound.md) tracks it.

### Per-domain routing — forward and/or webhook

Each `email_domains` row carries `inbound_enabled`, `forward_to`, and `webhook_url`. When `inbound_enabled` is true:

- `forward_to` (a list of addresses) triggers one outbound `Email` row per target through the existing send loop. The forward rewrites headers: envelope `From:` = `forwarder+<org>@mail.hail.so`, `Reply-To:` = the original sender, `References:` preserved for threading, and `X-Hail-Forward-Hops` plus `Auto-Submitted: auto-forwarded` for loop suppression.
- `webhook_url` triggers a signed POST through the webhook delivery worker.

A separate `inbound_routes` table, for per-mailbox routing in custom domains, is deferred to a future milestone (when tenants point their own MX at SES).

### Org-wide subscriptions

The `/webhooks` CRUD surface is the firehose pattern — one subscription covers multiple event types (`email.received`, `email.bounced`, `email.complained`). Signatures are Stripe-style: `X-Hail-Signature: t=<unix>,v1=<hex>`. Hail retries deliveries on a fixed `0/30s/2m/10m/1h/6h/24h` ladder. After the last retry, Hail marks the delivery `dead`. After 50 consecutive dead deliveries, the subscription auto-disables.

Refer to [AWS SES setup](./self-host/aws-ses.md) §10 for the operator runbook. Refer to [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](https://github.com/hail-hq/hail/blob/main/docs/superpowers/specs/2026-06-06-inbound-email-design.md) for the full spec.
