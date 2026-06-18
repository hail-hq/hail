# Changelog

All notable changes to Hail are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Hail adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.0] — 2026-06-12

Inbound email milestone. Hail can now receive mail at hail-mail addresses,
forward it, and fire webhooks on receipt and on bounce/complaint events.

Component versions cut alongside this umbrella release:
**`sdk-v0.3.0`** (PyPI: `hail-sdk==0.3.0`), **`cli-v0.5.0`** (Homebrew + GitHub Releases).

### Inbound email

- SES Receipt Rule → S3 (raw MIME) + Lambda (HMAC-signed JSON) → `POST /internal/ses-events` ingest pipeline.
- `Email.direction='inbound'` rows with `message_id`, `in_reply_to`, `references_ids`, `raw_s3_key`, and SES SPF/DKIM/DMARC/spam/virus verdicts persisted.
- `email_attachments` table — MIME attachments stored as separate S3 objects under `attachments/<email_id>/<att_id>`.
- Threading via `In-Reply-To` / `References` chains.
- Spam/virus `FAIL` verdicts persist the row but suppress forwarding and webhook fan-out.
- Idempotency via a partial unique index on `(organization_id, message_id) WHERE direction='inbound'` — duplicate SES deliveries short-circuit.

### Forwarding

- Per-domain `forward_to` list on `email_domains`. Inbound matches trigger one outbound row per target through the existing send loop.
- Header-rewrite forwarding (Cloudflare Email Routing-style): `From:` = `forwarder+<org>@<base_domain>` (SPF/DKIM aligned), `Reply-To:` = original sender, `References:` preserves threading.
- Loop guards: 3-hop max via `X-Hail-Forward-Hops`, refuse targets on `HAIL_MAIL_BASE_DOMAIN`, per-domain rate cap (default 200/hour, override via `forward_rate_per_hour`).

### Webhooks

- Org-wide `webhook_subscriptions` (multi-event firehose) + per-domain `email_domains.webhook_url` (single-target ergonomic). Both fire independently.
- Background `WebhookWorker` polls `webhook_deliveries` with `FOR UPDATE SKIP LOCKED`, signs (`X-Hail-Signature: t=<unix>,v1=<hmac-sha256>`), POSTs via httpx with a private-network guard.
- Retry ladder: 0s, 30s, 2m, 10m, 1h, 6h, 24h. After the last slot the row is `dead`; 50 consecutive deads auto-disable the subscription.
- `POST /webhooks/{id}/deliveries/{did}/redeliver` to replay a dead row.
- Webhook secrets are Fernet-encrypted at rest (`HAIL_WEBHOOK_SECRET_KEY`), so the worker survives an API restart without secret rotation.

### Schema rename: `sender_domains` → `email_domains`

- The table now carries both directions, so the historical "sender" name no longer fits. Renames the table, indexes, trigger, CHECK constraints, and the FK column on `emails`.
- Wire format: routes moved from `/sender-domains` → `/email-domains`; audit-action strings from `sender_domain.*` → `email_domain.*`; pydantic schemas `SenderDomain*` → `EmailDomain*`.

### API

- `GET /emails?direction=inbound|outbound` filter.
- `GET /emails/{id}/raw` and `GET /emails/{id}/attachments/{aid}` — 302 redirect to presigned S3 URLs (5-min TTL).
- `PATCH /email-domains/{id}` accepts inbound action fields (`inbound_enabled`, `forward_to`, `webhook_url`, `forward_rate_per_hour`). Setting `webhook_url` mints a secret returned once.
- `POST /email-domains/{id}/rotate-webhook-secret` rotates and returns the new plaintext once.
- Full `/webhooks` CRUD + `/rotate-secret` + `/deliveries` + `/deliveries/{id}/redeliver`.

### CLI (`cli-v0.5.0`)

- New `hail webhooks` subcommand tree (`create`, `list`, `deliveries`, `redeliver`).
- `hail email list --direction inbound` filter.
- `hail email sender-domain` renamed to `hail email email-domain` (alias `ed`); the legacy `sd` / `sender-domain` aliases are removed — **breaking** for users with scripts hitting the old name.
- Short-ID prefix resolution on every UUID-taking subcommand (was `call status` only): `email get`, `email-domain get/verify/delete`, `webhooks deliveries`, `webhooks redeliver` all accept the 4+ char hex prefix shown in `list` output, mirroring `git rev-parse`'s short-hash UX.

### SDK (`sdk-v0.3.0`)

- `client.webhooks.*` resource added.
- `client.emails.list(direction=...)` parameter added.
- `client.email_domains.patch()` accepts inbound action fields; `client.email_domains.rotate_webhook_secret(id)` added.
- `SenderDomain*` back-compat aliases removed (deprecated since `sdk-v0.2.0`) — **breaking** for users importing the legacy names.

### Infrastructure

- Terraform module under `infra/terraform/` provisions S3 (raw + attachments + lifecycle), SES Receipt Rule + Rule Set, Lambda + IAM + log group, and emits the MX record + the `set-active-receipt-rule-set` activation command.
- Stdlib-only `infra/ses-ingest-lambda/handler.py` bridges SES events into the HMAC-signed POST.
- Three migrations: `0006` (rename + auxiliary objects), `0007` (Email inbound columns + `email_attachments` + `email_domains` action columns + idempotency partial unique index), `0008` (`webhook_subscriptions` + `webhook_deliveries`). See `docs/operations.md` → "Inbound email rollout" for the staged deploy sequence.
- `InboundProvider` interface in `core/hailhq/core/providers/email/inbound/` with `SesInboundProvider` shipping and `SmtpInboundProvider` stubbed for a future cloud-agnostic milestone.
- Operator-facing env vars (`HAIL_INBOUND_EMAIL_NAME_PREFIX`, `HAIL_IAM_USER_NAME`) are required in `.env` — terragrunt fails fast on missing values instead of silently using a hardcoded default. The inbound bucket name is now a single source of truth (`Settings.hail_inbound_bucket` is a computed `${prefix}-raw`), so Terraform and the API can't drift.

### Deferred to next milestone

- Custom-domain inbound (tenant MX → SES) — schema is ready; the operator-side flow isn't.
- `inbound_routes` table for per-mailbox routing within custom domains.
- `SmtpInboundProvider` implementation (cloud-agnostic / OSS-only path).
- Reply-composition endpoint (`POST /emails/{id}/reply`).
- MCP push transport for inbound events.

[0.5.0]: https://github.com/hail-hq/hail/releases/tag/v0.5.0

## [0.1.0] — 2026-04-30

First public release. Outbound phone calls for AI agents, end-to-end.

### Phone calls

- Outbound calls via Twilio SIP through LiveKit Cloud.
- `POST /calls`, `GET /calls/{id}`, and `GET /calls` with cursor pagination.
- Per-org API-key auth with audit logging on every authenticated request.
- Idempotency-Key support on `POST /calls` (24h TTL).

### Voice pipeline

- Deepgram STT.
- Cartesia TTS (primary) with ElevenLabs fallback.
- Silero VAD (prewarmed once per worker process).
- LiveKit turn-detector for end-of-utterance detection.
- LLM system-prompt mode with OpenAI to Gemini to Anthropic fallback chain.
- LLM BYO-endpoint mode for any OpenAI chat-completions-compatible endpoint.
- Per-turn `call_events` rows for transcript reconstruction.

### Distribution

- OpenAPI 3.1 spec at `openapi/openapi.yaml` as the source of truth.
- `hail` CLI binary published via GitHub Releases (darwin and linux, amd64 and arm64) and a Homebrew tap.
- Remote MCP server bundled with every Hail deploy at `/sse`, exposing a `place_call` tool.
- `hail-sdk` on PyPI (imports as `hail`) with `Client.calls.create / get / list`.

### Infrastructure

- `docker compose up` brings up `api`, `voicebot`, `mcp`, and Postgres.
- Alembic migrations for the v1 schema.
- CI runs lint, pytest with a Postgres service container, Go build and test, and a docker-compose build smoke.

### Deferred to v1.x

- LiveKit Egress recording (`recording.py` is stubbed; `Call.recording_s3_key` is always `NULL`).
- `idempotency_keys` GC sweeper and in-flight reaper.
- Inbound calls (`LIVEKIT_SIP_INBOUND_TRUNK_ID` is reserved in env but unused).
- SMS channel (Twilio outbound and inbound).
- Email channel (AWS SES outbound and inbound).
- `CallEvent` dedupe across voicebot redispatch.

[0.1.0]: https://github.com/hail-hq/hail/releases/tag/v0.1.0
