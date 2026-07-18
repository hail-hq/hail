# Changelog

All notable changes to Hail are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and Hail adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.15.0] — 2026-07-18

Multi-country numbers milestone, contacts from the CLI, and a public SMS
pricing dataset.

Component versions cut alongside this release:
**`sdk-v0.11.0`** (PyPI: `hail-sdk==0.11.0`), **`cli-v0.14.0`** (Homebrew + GitHub Releases).

### Multi-country numbers

- `costs/telephony.json` grows a broad multi-country catalog: per-country
  number types, monthly prices, and per-row capabilities (voice/SMS), synced
  from Twilio's price lists by a scheduled CI job that opens a PR on drift.
- New `national` number type end to end: schema literal + migration, API,
  SDK (`NumberType` gains `national`), and `hail numbers acquire --type
national`.
- `POST /numbers` validates against the catalog allow-list: unlisted
  country/number-type combinations are rejected, and the acquired number's
  capabilities derive from the catalog row instead of being assumed. The
  catalog is bundled into the API image and read from the runtime path.
- `/costs` shows number capabilities across all countries.

### Contacts CLI

- `hail contacts list|create|update|delete|set-phone|clear-phone` — manage
  the org contact directory, including member phone numbers, from the CLI.
  OpenAPI client regenerated for the contacts + member-phone endpoints.

### SMS pricing dataset

- `costs/sms.json` — provider base rates for US outbound SMS per segment
  (Twilio, Vonage, Plivo, AWS End User Messaging, Telnyx) with per-row
  provenance and a JSON Schema; validated in CI and staleness-checked like
  the other costs files. Rates verified against official provider sources
  on 2026-07-17.
- `/costs` renders the dataset as a new SMS section and links the free
  tools at hail.so/tools that consume it.

### Fixed

- Number acquire guard hardened and capability derivation corrected
  (code review follow-ups to the multi-country work).
- Costs page and contacts CLI code-review fixes.

## [0.14.0] — 2026-07-17

Voicebot agent tools milestone. The voice agent can now act mid-call — text
the person it's talking to, email a directory contact, and hang up on its
own — through a channel-agnostic tool registry that picks up new modalities
automatically. Alongside: agent workspace self-signup with platform abuse
guardrails, and the contacts directory.

Component versions cut alongside this release:
**`sdk-v0.10.0`** (PyPI: `hail-sdk==0.10.0`), **`cli-v0.13.0`** (Homebrew + GitHub Releases).

### Voicebot agent tools

- Four in-call tools, on by default per configured channel: `send_sms` (call
  counterpart only), `send_email` (directory recipients only), `end_call`,
  and `list_contacts`. The agent never handles raw addresses — tool schemas
  accept directory names only; resolution happens server-side.
- `POST /calls` gains an optional `tools` field (omit = all available,
  `[]` = none, names validated against the registry) — propagated through
  the OpenAPI spec, SDK (`calls.create(tools=...)`), CLI
  (`hail call --tools end_call,send_sms`, `--tools none` to disable), and
  MCP `place_call`.
- Sends execute through HMAC-signed internal routes that reuse the full
  outbound stack — suppression/velocity gates, funds, audit
  (`agent.sms.send` / `agent.email.send`, plus `send_failed` reconciliation),
  AI-disclosure footer, and usage billing (same refs/rates as API sends).
- Guardrails: per-call send cap (5, serialized under a row lock with
  idempotent retries), platform agent caps + kill switch enforced on
  voicebot sends, confirm-before-send prompt rule, and a mandatory verbal
  goodbye before `end_call` deletes the room (actually dropping the SIP leg).

### Contacts directory

- Manual contacts and org-member phone numbers join the voicebot directory;
  `list_contacts` and email/SMS recipient resolution draw from both sources,
  always scoped to the calling org.

### Agent workspaces & abuse guardrails

- Agent-origin workspace self-signup, with per-recipient velocity caps,
  per-channel hourly/daily limits, and a platform kill switch on all
  agent-origin sends (`429` + `Retry-After` on the create routes).
- SMS pricing tiers: usage events now carry a per-destination tier
  (`sms:<id>:<tier>`) for corridor-accurate billing.

### SMS dedicated numbers & Sender ID

SMS dedicated numbers & Sender ID. Provision a number, attach it to a
per-org Messaging Service, and send to no-registration corridors via an
alphanumeric Sender ID with no dedicated number.

- `POST /numbers` (idempotent acquire), `GET /numbers`, `GET /numbers/{id}`;
  `POST /numbers/{id}/enable-sms` attaches the number to the org's Twilio
  Messaging Service.
- `GET`/`PATCH /sms/sender-id` — org-level custom alphanumeric Sender ID
  (2–11 chars). `POST /sms` now requires a dedicated number only for
  destinations that need one (US/Canada/India); UK and Germany send via the
  Sender ID with no dedicated number.
- CLI: `hail numbers acquire|list|get|enable-sms` and `hail sms sender-id
get|set`.
- SDK: `client.numbers` (acquire/list/get/enable_sms) and
  `client.sms.sender_id` (get/set).

## [0.13.1] — 2026-07-14

Bug fix. `cli-v0.12.1` cut alongside; the SDK is unchanged and stays at
`hail-sdk==0.9.0`.

- `hail email attachment-upload` and `hail email send` no longer swallow
  the server's real error message behind a raw JSON-unmarshal error when
  the API returns a plain-string `detail` (e.g. the attachment size-cap
  rejection) instead of FastAPI's list-shaped validation-error detail.

## [0.13.0] — 2026-07-14

Outbound email attachments. Upload a file once, reference its id from as
many sends as you like.

Component versions cut alongside this release:
**`sdk-v0.9.0`** (PyPI: `hail-sdk==0.9.0`), **`cli-v0.12.0`** (Homebrew + GitHub Releases).

- `POST /email-attachments` — upload a file (multipart/form-data,
  ≤10MB), get back a reusable id. `POST /emails` gains
  `attachment_ids` to attach one or more uploaded files; oversize
  requests (body + attachments combined, matching SES's 10MB raw-message
  cap) get a clear 422 suggesting a hosted link instead. Unused uploads
  are garbage-collected 24h after upload; used ones are kept
  indefinitely and reusable across sends.
- MCP: new `upload_email_attachment` tool; `send_email` gains
  `attachment_ids`.
- SDK: `client.email_attachments.create()`; `client.emails.create(...,
attachment_ids=...)`.
- CLI: `hail email attachment-upload <file>`; `hail email send` gains
  `--attach <file>` (upload + attach in one step) and `--attach-id <id>`.
- Internal: the S3 bucket/client backing inbound mail storage is renamed
  from "inbound" to a generic "mail" name (`HAIL_MAIL_NAME_PREFIX`
  replaces `HAIL_INBOUND_EMAIL_NAME_PREFIX`) since it now also holds
  outbound attachment uploads. No data migration; self-hosters recreate
  the bucket under the new prefix.

## [0.12.0] — 2026-07-10

SMS inbound & compliance milestone. Hail now receives inbound SMS, routes each
message to the owning organization, fans it out to that org's webhook
subscribers, and honors STOP/HELP/START opt-out signals — plus an
abuse-monitoring guardrail that protects the shared A2P 10DLC campaign.

Component versions cut alongside this release:
**`sdk-v0.8.0`** (PyPI: `hail-sdk==0.8.0`), **`cli-v0.11.0`** (Homebrew + GitHub Releases).

### Inbound SMS & opt-out

- `POST /sms/inbound` — the Twilio inbound webhook. Verified with Twilio's own
  `X-Twilio-Signature` (HMAC-SHA1) scheme against the configured public API
  URL, not the raw request URL, so it stays correct behind a TLS-terminating
  proxy. Returns `200` even for numbers Hail doesn't own, so Twilio never
  retries a drop.
- Org resolution is by the `To` number (dedicated numbers only, no pool
  fallback). Inbound and outbound number foreign keys are now split
  (`from_number_id` / `to_number_id`).
- Inbound messages are idempotent on Twilio's `MessageSid` (a retried delivery
  never creates a second row) and delivered to subscribers as an
  `sms.received` webhook event.
- Opt-out keywords write/clear rows in the shared `Suppression` list
  (`channel='sms'`, checked before every send): STOP-family opts out,
  START-family re-subscribes, HELP is recognized. Optional Hail-sent
  compliance auto-replies (`HAIL_SMS_COMPLIANCE_REPLIES_ENABLED`, off by
  default, with STOP/HELP/START templates) for deployments that disable
  Twilio's built-in opt-out handling.

### Suppression management

- `GET /sms/suppressions` (cursor-paginated, org-scoped) and
  `DELETE /sms/suppressions/{number}` — manual review and correction of the
  opt-out list. No MCP tool: suppression management is account configuration,
  not an agent-facing action.

### Abuse monitoring

- New `channel_suspensions` table and an `AbuseMonitorWorker` (runs in the API
  lifespan, hourly by default via `HAIL_ABUSE_MONITOR_POLL_SECONDS`). It
  computes each org's rolling SMS opt-out rate over a window and suspends the
  channel when it crosses a threshold (`HAIL_SMS_ABUSE_WINDOW_HOURS` /
  `_MIN_SENDS` / `_MAX_OPT_OUT_RATE`) — the mitigation for the shared-campaign
  risk where one org's abuse can get the whole platform throttled. A
  suspension blocks further SMS sends via `check_sms_allowed` until an operator
  lifts it. Thresholds are conservative starting values, expected to be tuned
  post-launch.

### CLI (`cli-v0.11.0`)

- New `hail sms suppressions list` / `hail sms suppressions delete <number>`.

### SDK (`sdk-v0.8.0`)

- `client.sms.list_suppressions` / `client.sms.delete_suppression` added.

## [0.11.0] — 2026-07-10

Bring-your-own provider keys. Cloud organizations can now supply their own API
keys and parameters for each layer of the voice pipeline — STT, LLM, and TTS —
instead of using Hail's bundled providers. Transport (Twilio + LiveKit) and the
pipeline stay Hail's; the brain and voice become yours.

No component versions were cut alongside this release: the feature is served
entirely by internal HMAC routes (`include_in_schema=False`), so the public
OpenAPI spec, CLI, and SDK are unchanged.

### BYO provider keys

- Per-organization provider config (`org_provider_config` table) storing keys
  **Fernet-encrypted at rest** under a dedicated `HAIL_PROVIDER_SECRET_KEY`.
  Keys are write-only — only a last-4 and a set-at timestamp are ever read
  back. Providers: STT (Deepgram), LLM (OpenAI-compatible / Anthropic /
  Google), TTS (Cartesia / ElevenLabs, with voice id).
- Managed via internal HMAC-signed routes under
  `/internal/orgs/{org}/providers` (list / upsert / delete / validate /
  activate). The customer-facing console lives in the hail-website repo.
- Runtime resolution: the voicebot loads and decrypts the org's **active**
  provider per layer at call time; precedence is per-call params → org config →
  deployment env. A per-call `llm` key is now encrypted in transit through
  LiveKit dispatch metadata rather than sent in plaintext.
- **Failure semantics:** a bad or revoked BYO key fails the call fast with a
  new `provider_key_error` end reason, unless the org opts into falling back to
  Hail's keys for that layer.

### Capability-based key validation

- The key check probes each provider's real capability endpoint with
  deliberately-invalid parameters, so auth **and** the specific permission are
  exercised without running (or billing) a synthesis/transcription. This
  catches granular-permission keys (common with ElevenLabs / Cartesia /
  Deepgram) that authenticate but lack the needed capability — which a plain
  auth probe silently passes.
- Tri-state result — **valid / invalid / couldn't-verify** — so a transient
  429 / 5xx / network error no longer reads as a bad key. Customer
  `openai-compatible` base URLs are SSRF-guarded (public HTTPS only, private/
  loopback/metadata ranges rejected).

### Multiple providers per layer

- A layer can hold several saved provider configs with exactly one **active**
  per layer, DB-enforced by a partial unique index. Switching the active
  provider is a single action; the voicebot always resolves the active one.
  Existing single-provider rows are backfilled to active.

## [0.10.0] — 2026-07-09

SMS outbound milestone. Hail can now send SMS via Twilio, gated by the same
consent, suppression, and velocity-cap infrastructure as calls and email.

Component versions cut alongside this umbrella release:
**`sdk-v0.7.0`** (PyPI: `hail-sdk==0.7.0`), **`cli-v0.10.0`** (Homebrew + GitHub Releases).

### SMS outbound

- `SmsProvider` interface in `core/hailhq/core/providers/sms/` with `TwilioSmsProvider` shipping.
- `Sms` model (`sms` table) and `SmsEvent` model (`sms_events` table, feeding the unified event stream below).
- Requires a dedicated, SMS-capable phone number on the organization — unlike calls, SMS does **not** fall back to a shared pool number, since a pool number cannot provide unambiguous number-to-org routing for a channel with no forced voice-menu context.
- A carrier-level rejection (e.g. an unregistered or filtered number) is recorded as a failed, unbilled send with the carrier's `error_code` — not a false "sent" status, and not charged.
- `Suppression.channel` CHECK widened to include `sms`; new `check_sms_allowed` gate mirrors the existing call/email checks. SMS velocity caps count attempted sends rather than billed sends, so a burst of carrier-rejected probing still trips the cap.
- **Audit-log shape change (calls):** generalizing the phone-channel compliance gate to cover SMS renamed the call `audit_log.payload.checks` keys `internal_dnc_checked` / `internal_dnc_hit` to `suppression_checked` / `suppression_hit` (voice and SMS now share one destination scrub). Update any external queries or dashboards that match the old key names; existing rows are not backfilled.
- SMS content is now covered by the existing DSAR export/delete and account-closure retention flows, on the same schedule as call transcripts and email content.

### API

- `POST /sms`, `GET /sms/{id}`, `GET /sms`.
- Unified `/events` stream (`GET /events`) extended to a three-way union across calls, email, and SMS; `EventResponse.source` now includes `sms`.
- Fixed: a same-idempotency-key retry of a request that failed pre-send validation (e.g. a malformed body, or a 403 from the consent/compliance/balance gates) previously replayed as "still processing" until the idempotency key expired, instead of replaying the original failure. Now also covers the per-call BYO `llm.base_url` safety rejection; the shared-pool-exhausted `503` on `/calls` releases the key instead, so a same-key retry can go through once capacity frees. Affects `/calls`, `/emails`, and `/sms`.
- Fixed: an outbound voice call with no explicit `from` could select an SMS-only number; `/calls` now resolves a from-number carrying the `voice` capability (mirroring `/sms`'s `sms` requirement) and falls back to the shared voice pool otherwise.

### CLI (`cli-v0.10.0`)

- New `hail sms send / status / list` subcommand.
- `hail tail` (unified event stream) surfaces `sms:` resource IDs alongside `call:` and `email:`.

### SDK (`sdk-v0.7.0`)

- `client.sms.create / get / list` resource added.
- Resource-ID parsing (`client.events.get`) widened to accept `sms:` alongside `call:` and `email:`.

### MCP

- `send_sms`, `get_sms`, `list_sms` tools added (eleven → fourteen tools total).

### Deferred to next milestone

- Inbound SMS: webhook receipt, STOP/HELP/START opt-out keyword processing.
- Self-serve dedicated-number provisioning — today, assigning a number to an organization is a manual database operation; see `docs/operations.md`.
- Custom per-org Sender ID (international, outbound-only corridors).
- Console UI for SMS activity, numbers, and suppression management.

[0.10.0]: https://github.com/hail-hq/hail/releases/tag/v0.10.0

## [0.9.1] — 2026-07-08

Component version cut alongside this release: **`cli-v0.9.1`** (Homebrew + GitHub Releases). No SDK change — `sdk-v0.6.0` is unaffected and still current.

- Fixed a crash in the CLI (and any other strictly-typed OpenAPI client) when
  the API rejected a request with a 422 whose `detail` was a plain string
  instead of the OpenAPI-documented `HTTPValidationError` list shape —
  surfaced as `json: cannot unmarshal string into Go struct field
HTTPValidationError.detail of type []client.ValidationError` instead of
  the actual validation message. Affected `POST /calls`, `POST /emails`
  (including `/emails/stats`), and the webhooks endpoints; all hand-raised
  422s across the API now go through a shared helper that matches the
  documented shape.
- The CLI now checks `--recipient-consent` client-side before making any
  request: previously a bare bool flag defaulting `false` with no local
  check, so an omitted or explicitly-`false` value always round-tripped to
  the server before failing. Same treatment for `call`'s `--prompt`/
  `--llm-*` mode group and `email send`'s `--body`/`--body-html`/
  `--body-file`/`--body-html-file` group, plus previously-unenforced
  conditional requirements (`--consent-source` when
  `--message-type=marketing`, `--domain` when `--kind=custom`).
- `--help` output now visually distinguishes unconditionally-required flags
  (`Required flags:`) and one-of-N groups (`Required (one of):`) from
  genuinely optional ones, instead of one undifferentiated alphabetical
  list — falls back to the prior single `Flags:` block for every command
  with neither.

## [0.9.0] — 2026-07-07

**Breaking change.** Component versions cut alongside this release:
**`sdk-v0.6.0`** (PyPI: `hail-sdk==0.6.0`), **`cli-v0.9.0`** (Homebrew + GitHub Releases).

- `POST /calls` and `POST /emails` now **require** a `recipient_consent: bool`
  field (rejected with 422 if missing or `false`). This attests that the
  caller has obtained the lawful consent required to contact the recipient —
  Hail does not verify consent itself. Optional `consent_source` and
  `consent_obtained_at` fields record how/when consent was obtained; a
  required non-empty `consent_source` is enforced when the new optional
  `message_type: "marketing"` field is set (default remains
  `"informational"`).
  - **Every existing integration must add `recipient_consent: true` (or the
    real attestation) to its calls/emails, or requests will start failing
    with 422 the moment the API deploys this change.** The CLI (`--recipient-consent`),
    SDK (`recipient_consent=` kwarg), and MCP tools (`recipient_consent`
    param) all already require and forward it as of this release — pin to
    `hail-sdk>=0.6.0` and `cli-v0.9.0`+ before or at the same time as
    upgrading against a Hail API instance running this change.
- Outbound voice calls now open with a fixed, non-configurable AI-disclosure
  line spoken before anything else; outbound email carries an equivalent
  disclosure appended at send time, outside the caller-authored body.
- New global suppression list + one-click email unsubscribe
  (`List-Unsubscribe`/`List-Unsubscribe-Post` headers); a pre-send
  compliance gate blocks suppressed recipients, premium-rate destinations,
  and per-org rate limits before any call or email goes out.
- New data-retention purge job and DSAR (lookup/export/delete) tooling for
  recipient data requests.
- `/legal/*` pages (Terms of Use, AUP, Privacy Policy, DPA, sub-processors,
  Cookie Policy) published on hail-website.

## [0.8.1] — 2026-07-02

Internal consolidation release. `cli-v0.8.1` cut alongside; the SDK is
unchanged and stays at `hail-sdk==0.5.0`.

- One shared cursor-pagination helper now backs every paginated API route
  (was seven hand-copies) and one generic page-walker backs all three CLI
  list commands. Wire contract unchanged, with one edge unified: an
  empty-string `cursor` query param is now ignored everywhere instead of
  returning 400 on some routes.
- `hail email events --all` now emits the same walked-items stderr warning
  as `call list --all` and `email list --all`.

## [0.8.0] — 2026-07-02

Component versions cut alongside this umbrella release:
**`sdk-v0.5.0`** (PyPI: `hail-sdk==0.5.0`), **`cli-v0.8.0`** (Homebrew + GitHub Releases).

- `GET /emails/{id}/events` is now cursor-paginated (`cursor`, `limit` 1..1000,
  `next_cursor` in the response). `hail email events` gains
  `--cursor/--limit/--all`; SDK `emails.events()` and MCP `get_email_events`
  accept `cursor`/`limit`. Existing callers are unaffected (default limit 100).

## [0.7.0] — 2026-07-02

Email deliverability milestone. Every outbound email now has a tracked
lifecycle — delivered, delayed, bounced, complained, rejected, opened,
clicked — visible per message, aggregated per account, and pushed to
customer webhooks.

Component versions cut alongside this umbrella release:
**`sdk-v0.4.0`** (PyPI: `hail-sdk==0.4.0`), **`cli-v0.7.0`** (Homebrew + GitHub Releases).

### Deliverability tracking

- New `email_events` table (append-only, migration `0020`); outbound sends write a synthetic `sent` event, and SES configuration-set events (Delivery, Bounce, Complaint, Reject, DeliveryDelay, Open, Click) are ingested via SNS → ingest Lambda → `POST /internal/ses-events`. Duplicate SNS deliveries are absorbed by a dedup constraint; status transitions are guarded so terminal states never regress. `emails.status` gains `delivered`.
- `GET /emails/{id}/events` — per-email lifecycle timeline. `GET /emails/{id}` gains `last_event_at`.
- `GET /emails/stats?from=&to=&bucket=hour|day` — account-level counts, rates (delivery, hard-bounce, complaint, unique open/click), and a zero-filled time series.
- Email events join the unified `GET /events` stream (`source: "email"`, `id=email:<uuid>` filter).
- Webhooks: `email.delivered`, `email.delivery_delayed`, `email.opened`, `email.clicked` are new; `email.bounced` and `email.complained` now actually fire.
- CLI: `hail email events <id>`, `hail email stats`. SDK: `client.emails.events()`, `client.emails.stats()`. MCP: `get_email_events`, `get_email_stats`.
- Infra: SES configuration set (`HAIL_SES_CONFIGURATION_SET`, Terraform default `hail-events`), SNS topic with explicit SES publish policy, subscription DLQ. Open/click tracking uses the default SES tracking domain in v1.

## [0.6.0] — 2026-06-28

Custom sender domains milestone. Tenants can now send and receive on their own
domain, with DKIM and a custom MAIL FROM configured automatically.

### Custom sender domains — send and receive on your own domain

- Register a tenant-controlled domain: `POST /email-domains` with `kind=custom` (or `hail email domain register --kind custom --domain acme.com`). Hail calls SES `CreateEmailIdentity` **and auto-configures a custom MAIL FROM** on `send.<domain>` via `PutEmailIdentityMailFromAttributes` — no AWS-console step for the tenant.
- The registration response returns the full DNS record set to publish: three DKIM `_domainkey` CNAMEs **plus** the MAIL FROM `send.<domain>` MX (`feedback-smtp.<region>.amazonses.com`) and SPF TXT (`v=spf1 include:amazonses.com ~all`). `DnsRecord` gains an optional `priority` for MX rows.
- `POST /email-domains/{id}/verify` re-polls SES for **both** DKIM and MAIL FROM, surfacing `mail_from_status` alongside `verification_status`. On-demand only — clients/console re-poll; there is no background poller.
- Receive inbound mail on **verified** custom domains. Inbound is matched by the owning identity, so a message to two of an org's domains yields one inbound row + webhook **per matched domain**, with the domain name on the event payload.
- New IAM grant `ses:PutEmailIdentityMailFromAttributes` for the production identity (`infra/terraform/iam.tf`) — required for the custom MAIL FROM call.

### Auth

- The managed console now authenticates to the API with a short-lived Better Auth **session JWT** (carrying `sub` and the session `activeOrganizationId`) rather than a cached per-browser API key. The API resolves the request's organization from the active-org claim, validated against membership — closing a cross-tenant write path.

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
