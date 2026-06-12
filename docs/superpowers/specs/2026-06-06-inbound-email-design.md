# Inbound email — design

Status: draft
Owners: r13i

## Goals & non-goals

**Goal.** Receive email at Hail-controlled addresses, persist it alongside outbound, and let tenants react to it via two ergonomics:

- **Forwarding** — Hail re-sends the message to one or more addresses configured on the destination email domain.
- **Webhooks** — Hail POSTs an event to URLs configured per-domain and/or on org-wide subscriptions.

A tenant can use either, both, or neither.

**Non-goals (this milestone).**

- Inbound on tenant-controlled custom domains (MX delegation). Deferred to the next milestone — see §10.
- A non-AWS ingestion provider. The `InboundProvider` interface lands so the future `SmtpInboundProvider` has a slot, but only `SesInboundProvider` ships now.
- Web UI / console. Forbidden by repo tenet.
- DMARC enforcement on inbound. We record verdicts; we accept everything.
- Reply-composition endpoint, server-side templates, attachment AV beyond what SES tags, reply-suggestion AI.

## 1. Architecture overview

```
incoming SMTP ──► SES Receipt Rule (managed by Terraform)
                  ├─ Action 1: S3   → s3://${HAIL_INBOUND_BUCKET}/raw/${messageId}
                  └─ Action 2: Lambda invoke (SES event payload)
                                          │
                                          ▼
                                ses-ingest-lambda (Python, stdlib only)
                                  • lift HMAC secret from env
                                  • sign + POST a small JSON to Hail API
                                          │
                                          ▼
                          POST ${HAIL_API_URL}/internal/ses-events
                          X-Hail-Signature: sha256=...
                          { message_id, envelope_from, recipients[],
                            verdicts{spam,virus,spf,dkim,dmarc},
                            s3_bucket, s3_key, timestamp }
                                          │
                                          ▼
                          Hail API ingest handler:
                            1. verify HMAC, dedupe by message_id
                            2. fetch raw MIME from S3
                            3. parse MIME, extract attachments
                            4. route to org by parsing local-part
                            5. persist Email row(s) (direction='inbound')
                            6. fan out:
                               - per-domain forward (re-send via outbound path)
                               - per-domain webhook
                               - org-wide subscriptions
```

The rest of the pipeline downstream of `/internal/ses-events` is provider-neutral. When `SmtpInboundProvider` ships later, it produces the same internal `InboundMessage` payload and joins the pipeline at step 3.

## 2. Provider interface

A new abstraction sits next to `EmailProvider` in `core/hailhq/core/providers/email/`:

```python
# core/hailhq/core/providers/email/inbound/base.py

class InboundMessage(BaseModel):
    """Provider-neutral parsed inbound envelope."""
    provider_message_id: str          # SES Message-ID, future SMTP queue-ID
    envelope_from: str
    envelope_recipients: list[str]
    raw_s3_bucket: str
    raw_s3_key: str
    spam_verdict: str | None
    virus_verdict: str | None
    spf_verdict: str | None
    dkim_verdict: str | None
    dmarc_verdict: str | None
    received_at: datetime

class InboundProvider(ABC):
    """How raw MIME reaches Hail. Implementations parse provider-native
    notifications into InboundMessage and hand them to the common pipeline."""

    @abstractmethod
    async def verify_notification(self, headers: Mapping[str, str], body: bytes) -> bool:
        """Validate the notification came from the provider (HMAC, SNS sig, etc.)."""

    @abstractmethod
    async def parse_notification(self, body: bytes) -> InboundMessage:
        """Decode the provider's notification format into InboundMessage."""
```

Shipped implementations: `SesInboundProvider` (in `inbound/ses.py`).
Future stub: `SmtpInboundProvider` (in `inbound/smtp.py`) — empty module with a `NotImplementedError` and a docstring linking the placeholder doc at `docs/setup/smtp-inbound.md`.

## 3. Database schema

One migration, ordered by destructiveness:

### 3.1 Rename `sender_domains` → `email_domains`

```sql
ALTER TABLE sender_domains RENAME TO email_domains;
ALTER INDEX sender_domains_pkey                 RENAME TO email_domains_pkey;
ALTER INDEX sender_domains_org_domain_uq        RENAME TO email_domains_org_domain_uq;
-- ...all indexes & constraints renamed in lockstep
ALTER TABLE emails RENAME COLUMN sender_domain_id TO email_domain_id;
ALTER TABLE emails RENAME CONSTRAINT emails_sender_domain_id_fkey
                               TO   emails_email_domain_id_fkey;
```

ORM `SenderDomain` → `EmailDomain` in `core/hailhq/core/models.py`. All call-sites in `api/`, tests, and the OpenAPI spec follow. CLI surface `/sender-domains` → `/email-domains`; SDK / CLI bump to the next minor version.

### 3.2 Inbound fields on `emails`

```sql
ALTER TABLE emails
  ADD COLUMN direction text NOT NULL DEFAULT 'outbound',
  ADD CONSTRAINT emails_direction_check CHECK (direction IN ('outbound','inbound')),
  ALTER COLUMN email_domain_id DROP NOT NULL,
  ADD COLUMN provider_received_at timestamptz NULL,
  ADD COLUMN message_id text NULL,
  ADD COLUMN in_reply_to text NULL,
  ADD COLUMN references_ids text[] NULL,
  ADD COLUMN raw_s3_key text NULL,
  ADD COLUMN spam_verdict text NULL,
  ADD COLUMN virus_verdict text NULL,
  ADD COLUMN dkim_verdict text NULL,
  ADD COLUMN spf_verdict text NULL,
  ADD COLUMN dmarc_verdict text NULL;

ALTER TABLE emails DROP CONSTRAINT emails_status_check;
ALTER TABLE emails ADD CONSTRAINT emails_status_check
  CHECK (status IN ('queued','sent','failed','bounced','complained','received'));

ALTER TABLE emails ADD CONSTRAINT emails_outbound_has_domain
  CHECK (direction = 'inbound' OR email_domain_id IS NOT NULL);

CREATE INDEX emails_org_direction_created_idx
  ON emails (organization_id, direction, created_at DESC);
CREATE INDEX emails_message_id_idx ON emails (message_id);
CREATE UNIQUE INDEX emails_inbound_message_id_uq
  ON emails (organization_id, message_id)
  WHERE direction = 'inbound' AND message_id IS NOT NULL;
```

The unique partial index is the source of truth for ingest idempotency:
re-delivering the same SES `messageId` raises a constraint violation that
the handler swallows.

`status='received'` is the inbound terminal state. Bounce/complaint events continue to update the matching outbound row (looked up by `provider_message_id`).

### 3.3 Inbound action fields on `email_domains`

```sql
ALTER TABLE email_domains
  ADD COLUMN inbound_enabled       boolean NOT NULL DEFAULT false,
  ADD COLUMN forward_to            text[]  NULL,
  ADD COLUMN webhook_url           text    NULL,
  ADD COLUMN webhook_secret_hash   text    NULL,             -- bcrypt
  ADD COLUMN forward_rate_per_hour integer NULL,
  ADD CONSTRAINT email_domains_inbound_action CHECK (
    NOT inbound_enabled
    OR forward_to IS NOT NULL
    OR webhook_url IS NOT NULL
  ),
  ADD CONSTRAINT email_domains_webhook_pair CHECK (
    (webhook_url IS NULL) = (webhook_secret_hash IS NULL)
  );
```

Per-mailbox routing (`support@` vs `sales@`) doesn't fit this row shape. When custom-domain inbound ships (§10) it brings an `inbound_routes` table; the columns above become the catchall `*` route on backfill.

### 3.4 Attachments

```sql
CREATE TABLE email_attachments (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  email_id      uuid NOT NULL REFERENCES emails(id) ON DELETE CASCADE,
  filename      text NOT NULL,
  content_type  text NOT NULL,
  size_bytes    integer NOT NULL,
  s3_key        text NOT NULL,
  content_id    text NULL,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX email_attachments_email_id_idx ON email_attachments (email_id);
```

Stored under `s3://${HAIL_INBOUND_BUCKET}/attachments/${email_id}/${attachment_id}` so the API can issue presigned GETs without re-parsing raw MIME.

### 3.5 Webhook subscriptions and deliveries

```sql
CREATE TABLE webhook_subscriptions (
  id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  organization_id      uuid NOT NULL,
  target_url           text NOT NULL,
  secret_hash          text NOT NULL,                         -- bcrypt
  event_types          text[] NOT NULL CHECK (cardinality(event_types) >= 1),
  status               text NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active','disabled')),
  consecutive_failures integer NOT NULL DEFAULT 0,
  last_success_at      timestamptz NULL,
  last_failure_at      timestamptz NULL,
  created_at           timestamptz NOT NULL DEFAULT now(),
  updated_at           timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
  id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  subscription_id uuid NULL REFERENCES webhook_subscriptions(id) ON DELETE CASCADE,
  email_domain_id uuid NULL REFERENCES email_domains(id) ON DELETE CASCADE,
  event_type      text NOT NULL,
  event_id        uuid NOT NULL,
  payload         jsonb NOT NULL,
  attempt         integer NOT NULL DEFAULT 0,
  status          text NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','succeeded','failed','dead')),
  response_status integer NULL,
  response_body   text NULL,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  succeeded_at    timestamptz NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  CHECK (subscription_id IS NOT NULL OR email_domain_id IS NOT NULL)
);

CREATE INDEX webhook_deliveries_pending_idx
  ON webhook_deliveries (next_attempt_at)
  WHERE status = 'pending';
```

A delivery row belongs to either an org-wide subscription or a per-domain webhook (mutually exclusive via the CHECK). Both paths share the same retry/replay machinery.

## 4. API surface

Tenant-facing:

```
GET    /emails                                   # existing; add ?direction filter
GET    /emails?direction=inbound                 # canonical inbox listing
GET    /emails/{id}                              # existing; serves both directions
GET    /emails/{id}/raw                          # 302 → presigned S3 URL (5-min TTL); 404 for outbound
GET    /emails/{id}/attachments/{aid}            # 302 → presigned S3 URL (5-min TTL)

GET    /email-domains                            # rename
POST   /email-domains
GET    /email-domains/{id}
PATCH  /email-domains/{id}                       # accepts inbound_enabled,
                                                 # forward_to, webhook_url
DELETE /email-domains/{id}
POST   /email-domains/{id}/verify                # existing
POST   /email-domains/{id}/rotate-webhook-secret # returns plaintext once

POST   /webhooks
GET    /webhooks
GET    /webhooks/{id}
PATCH  /webhooks/{id}
DELETE /webhooks/{id}
POST   /webhooks/{id}/rotate-secret
GET    /webhooks/{id}/deliveries
POST   /webhooks/{id}/deliveries/{did}/redeliver
```

Internal (operator-side):

```
POST   /internal/ses-events                      # Lambda → API, HMAC-signed
```

Mounted on the existing `internal_router` (mirrors `internal_webhook`). Not exposed in the public OpenAPI spec.

CLI mirrors:

```
hail email list --direction inbound
hail email domain set-forward    <id> --to ops@acme.com,billing@acme.com
hail email domain set-webhook    <id> --url https://...
hail email domain rotate-secret  <id>
hail webhooks create --url ... --events email.received,email.bounced
hail webhooks deliveries <id>
hail webhooks redeliver  <id> <delivery_id>
```

URLs (target URLs, S3 presigns, internal endpoint base) flow through
`hailhq.core.urls` helpers per the URL-handling invariant in CLAUDE.md. No
ad-hoc f-string concatenation.

## 5. Webhook delivery

### 5.1 Signing scheme

`X-Hail-Signature: t=<unix_ts>,v1=<hex_hmac_sha256>` over `f"{t}.{raw_body}"` with the subscription or per-domain secret. Stripe-style. Tenants verify by recomputing and comparing in constant time.

Other headers per request:

- `X-Hail-Event: email.received` (or `email.bounced`, `email.complained`)
- `X-Hail-Delivery: <delivery.id>`
- `X-Hail-Subscription: <subscription.id>` (when applicable)
- `X-Hail-Email-Domain: <email_domain.id>` (when applicable)

### 5.2 Payload shape

```json
{
  "id": "<delivery.id>",
  "type": "email.received",
  "api_version": "2026-06-06",
  "created_at": "2026-06-07T10:11:12Z",
  "organization_id": "...",
  "data": {
    "id": "<email.id>",
    "direction": "inbound",
    "from_address": "...",
    "to_addresses": [...],
    "subject": "...",
    "message_id": "...",
    "in_reply_to": "...",
    "spam_verdict": "PASS",
    "virus_verdict": "PASS",
    "spf_verdict": "PASS",
    "dkim_verdict": "PASS",
    "dmarc_verdict": "PASS",
    "raw_url": "https://api.hail.so/emails/.../raw",
    "attachments": [
      {"id": "...", "filename": "...", "content_type": "...", "size_bytes": 1234,
       "url": "https://api.hail.so/emails/.../attachments/..."}
    ]
  }
}
```

`raw_url` and attachment URLs are stable Hail API endpoints that 302-redirect to presigned S3 URLs on access. The presigned target has a 5-minute TTL — short enough that leaked logs don't matter, long enough for a normal handler to fetch. The payload-side URLs themselves don't expire; the tenant can re-fetch as long as the row exists, subject to API-level auth.

### 5.3 Retry schedule

Attempts at: 0s, 30s, 2min, 10min, 1h, 6h, 24h. After 7 failures the delivery is marked `dead`. A subscription's `consecutive_failures` increments per dead delivery; at 50 the subscription auto-disables. Operator re-enables via PATCH.

### 5.4 Delivery worker

Single background asyncio task in the `api` service. Polls `webhook_deliveries WHERE status='pending' AND next_attempt_at <= now()`, claims rows with `FOR UPDATE SKIP LOCKED`, POSTs, updates. Semaphore-capped concurrency (default 32). Horizontal scaling deferred — one worker fits v1 volumes.

## 6. Forwarding

Forwarding is an outbound send triggered by the ingest pipeline. After persisting the inbound `Email` row:

1. Look up the destination `email_domains` row.
2. If `inbound_enabled` is false → stop.
3. If `forward_to` is non-empty → for each address, enqueue an outbound `Email` row through the existing send path. The outbound worker handles delivery via `SesEmailProvider`.

### 6.1 Header rewrite

The forwarded message is **sent as Hail** with `Reply-To:` carrying the original sender. This keeps SPF and DKIM aligned (Hail signs, Hail's domain is the envelope sender) without SRS.

```
From:           forwarder+<org>@mail.hail.so
Reply-To:       <original From: address>
To:             <forward target>
Subject:        Fwd: <original subject>
References:     <original Message-ID>
X-Hail-Forwarded-From:        <original From: address>
X-Hail-Original-Message-Id:   <original Message-ID>
X-Hail-Inbound-Id:            <hail Email.id>
X-Hail-Forward-Hops:          <hops>
Auto-Submitted:               auto-forwarded
```

Body: original `text/plain` + `text/html` parts preserved verbatim, prefixed with a standard "Forwarded message" preamble showing original `From`/`Date`/`Subject`. Attachments re-attached from S3.

### 6.2 Loop prevention

- If the inbound carries `X-Hail-Forward-Hops` ≥ `HAIL_FORWARD_MAX_HOPS` (default 3), persist the row but skip forwarding. Emit `email.received.suppressed` with `reason='forward_loop'` on the wire.
- Reject forward targets whose domain matches `HAIL_MAIL_BASE_DOMAIN`.
- Per-domain rate cap: `email_domains.forward_rate_per_hour` (nullable; falls back to `HAIL_FORWARD_RATE_PER_HOUR`, default 200). Beyond the cap, persist but skip forward and emit `email.received.suppressed` with `reason='forward_rate_limit'`.

### 6.3 Multi-recipient inbound

A single SES delivery to several recipients on `mail.hail.so` becomes:

- One `Email` row per **org** addressed (different orgs → different rows, all pointing at the same `raw_s3_key`).
- For each row, forwarding follows the row's `email_domains` configuration.
- Webhook fan-out fires per row.

Recipients outside any known org (no matching local-part suffix) are dropped — `postmaster@`, `abuse@`, mistyped addresses. Logged at INFO; no row written.

## 7. Security

- **HMAC verification.** `/internal/ses-events` requires `X-Hail-Signature: sha256=<hex>` over the raw request body, computed with `HAIL_INBOUND_HMAC_SECRET`. Constant-time comparison. 401 on mismatch.
- **Idempotency.** SES `messageId` is unique; the ingest handler short-circuits if an `emails` row with that `message_id` already exists for the org.
- **Spam/virus suppression.** When `spam_verdict='FAIL'` or `virus_verdict='FAIL'`, persist with `status='received'` and `metadata.suppressed = 'spam' | 'virus'`. Skip forwarding and skip webhook fan-out. Tenant can still see the row via `GET /emails`.
- **Rate limit on inbound per org.** Soft cap (default 1000/hour). Beyond it, persist but skip fan-out, emit `email.received.suppressed` with `reason='inbound_rate_limit'`.
- **Webhook target URL allowlist.** HTTPS required. Localhost / RFC-1918 / link-local rejected unless `HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=true` (self-host convenience).
- **Per-org secret on `/internal/ses-events`.** Single shared secret for the milestone; per-source-Lambda rotation in a follow-up if we end up running multiple ingest paths.

## 8. Operator setup — Terraform + Lambda

### 8.1 Repo layout

```
infra/
├── terraform/
│   ├── versions.tf            # provider + Terraform version pins
│   ├── variables.tf           # hail_api_url, hmac_secret, base_domain, region
│   ├── main.tf                # provider block, locals
│   ├── s3_inbound.tf          # bucket + lifecycle (90d) + SES write policy
│   ├── ses_inbound.tf         # receipt rule set, rule, MX output
│   ├── lambda_ingest.tf       # function, role, log group, env wiring
│   └── outputs.tf             # MX value, bucket name, lambda ARN
└── ses-ingest-lambda/
    ├── handler.py             # ~40 lines, stdlib only
    └── README.md
```

### 8.2 Lambda handler (canonical sketch)

```python
import hashlib, hmac, json, os, urllib.request

def handler(event, _ctx):
    record = event["Records"][0]["ses"]
    payload = {
        "message_id": record["mail"]["messageId"],
        "envelope_from": record["mail"]["source"],
        "recipients": record["receipt"]["recipients"],
        "verdicts": {
            "spam":   record["receipt"]["spamVerdict"]["status"],
            "virus":  record["receipt"]["virusVerdict"]["status"],
            "spf":    record["receipt"]["spfVerdict"]["status"],
            "dkim":   record["receipt"]["dkimVerdict"]["status"],
            "dmarc":  record["receipt"]["dmarcVerdict"]["status"],
        },
        "s3_bucket": os.environ["HAIL_INBOUND_BUCKET"],
        "s3_key":    f"raw/{record['mail']['messageId']}",
        "timestamp": record["mail"]["timestamp"],
    }
    body = json.dumps(payload).encode()
    sig = hmac.new(
        os.environ["HAIL_INBOUND_HMAC_SECRET"].encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    req = urllib.request.Request(
        f"{os.environ['HAIL_API_URL']}/internal/ses-events",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-Hail-Signature": f"sha256={sig}",
        },
        method="POST",
    )
    urllib.request.urlopen(req, timeout=10)
```

No third-party packages: stdlib `urllib` + `hmac` + `json`. Zip size measured in KB.

### 8.3 Deploy flow

```bash
cd infra/terraform
cp hail.tfvars.example hail.tfvars     # fill in api_url, base_domain, hmac_secret
terraform init
terraform plan
terraform apply

# Outputs include:
#   inbound_mx_record    = "10 inbound-smtp.us-east-1.amazonaws.com"
#   inbound_bucket       = "hail-inbound-prod-xxxx"
#   lambda_function_arn  = "arn:aws:lambda:us-east-1:..."
```

Then:

1. Publish the `inbound_mx_record` at the DNS provider for `HAIL_MAIL_BASE_DOMAIN`.
2. Activate the receipt rule set (see §8.4).
3. Set `HAIL_INBOUND_ENABLED=true` and `HAIL_INBOUND_HMAC_SECRET=...` in the API service `.env`. Restart `api`.
4. Send a test mail to a hail-mail address. Confirm with `hail email list --direction inbound` that the row appears.

### 8.4 Receipt rule set activation — known SES wart

SES has **one active receipt rule set per region per AWS account.** The Terraform module _creates_ the rule set; it does not activate it (activation is destructive when an account already has one running).

- **Greenfield account:** `aws sesv2 set-active-receipt-rule-set --rule-set-name hail-inbound`.
- **Account with existing rules:** import the existing rule set into Terraform state and merge the Hail rule into it, or skip the module's rule resource and add the rule manually via the AWS console.

Documented in `docs/setup/aws-ses.md` under a new "Inbound" section.

### 8.5 New env vars

Added to `.env.example` under the AWS SES section:

```bash
HAIL_INBOUND_ENABLED=false
HAIL_INBOUND_BUCKET=
HAIL_INBOUND_HMAC_SECRET=
HAIL_FORWARD_MAX_HOPS=3
HAIL_FORWARD_RATE_PER_HOUR=200
HAIL_WEBHOOK_ALLOW_PRIVATE_NETWORKS=false
```

## 9. Testing

- **Lambda handler:** unit test with a fixture SES event, assert the signed POST body matches expectations. No AWS calls.
- **Provider parsing:** fixture SES notification + raw MIME files → `SesInboundProvider.parse_notification` produces an `InboundMessage`. Includes UTF-8 subject, multipart, large attachment, nested `message/rfc822`, missing `Message-ID`.
- **`/internal/ses-events`:** golden path; HMAC failure (401); replay (idempotent); spam-verdict suppression; rate-limited path.
- **Routing:** addressing fixtures verifying `alice+acme@mail.hail.so` lands in the `acme` org, `unknown@mail.hail.so` is dropped, `postmaster@mail.hail.so` is dropped.
- **Forwarding:** verify header rewrite (From/Reply-To/References/X-Hail-\* set correctly), loop counter increments and suppresses at the max, base-domain self-forward rejected, rate cap suppresses correctly.
- **Webhook delivery:** signature round-trip; retry-schedule math; auto-disable at 50 consecutive failures; redeliver replays a `dead` row.
- **Migration:** round-trip on a non-empty DB. Existing rows pick up `direction='outbound'`, `email_domain_id` stays populated for outbound. `sender_domains` → `email_domains` rename does not break any existing query.
- **Schema rename smoke:** `pytest -k sender_domain` should find zero matches after the rename pass.

## 10. Next milestone (sketched)

Out of scope here; defined here so the door is clear:

- **Custom-domain inbound.** Tenants opt their own DNS-controlled domain into receiving by pointing MX at SES via a guided flow. Adds a per-domain `inbound_mx_record` field surfaced for the tenant to publish, and an SES receipt rule per opted-in custom domain.
- **`inbound_routes` table.** Per-mailbox routing within a custom domain (`support@acme.com` vs `sales@acme.com`). The current per-domain columns on `email_domains` become the catchall `*` route on backfill.
- **`SmtpInboundProvider`.** Cloud-agnostic / OSS path. Container `mailbot/` parallel to `voicebot/`, `aiosmtpd`-backed. Documented production recipe: front with Maddy or Postfix for verification and flood resistance.
- **Reply-composition endpoint.** `POST /emails/{id}/reply` builds threaded outbound from an inbound row.
- **MCP push transport.** Stream inbound events to agent clients connected over MCP.

## 11. Out of scope (this and next milestones)

- Web UI / dashboard.
- Server-side templates (`POST /emails` continues to take raw `body_text`/`body_html`).
- DMARC enforcement on inbound (verdicts recorded only).
- Attachment AV scanning beyond what SES tags.
- Reply-suggestion AI.
- IMAP fetch API.
- Multi-region SES deployment (the operator picks one region).
- Inbound for SMS or voice — separate channel milestones.

## 12. Nice-to-have

Land during this milestone only if cheap; otherwise defer:

- DMARC verdict surfaced in the API even when enforcement stays off.
- Wildcard event-type filters on webhook subscriptions (`email.*`).
- Per-subscription / per-domain retry-schedule overrides.
- Delivery replay from a cursor (replay all `dead` since timestamp X).
- SRS forwarding option (alongside header-rewrite) for tenants who need original-`From:` preservation.
- AWS CDK alternative module mirroring the Terraform one, for shops standardized on CDK.
- Forward target round-trip verification (send a confirmation token to the target before activating it as a forward destination).

## References

- Outbound email setup: [docs/setup/aws-ses.md](../../setup/aws-ses.md)
- `EmailProvider` (outbound): [core/hailhq/core/providers/email/base.py](../../../core/hailhq/core/providers/email/base.py)
- `SesEmailProvider`: [core/hailhq/core/providers/email/ses.py](../../../core/hailhq/core/providers/email/ses.py)
- `/emails` route: [api/hailhq/api/routes/emails.py](../../../api/hailhq/api/routes/emails.py)
- URL helpers (per CLAUDE.md): [core/hailhq/core/urls.py](../../../core/hailhq/core/urls.py)
- OpenAPI source of truth: [openapi/openapi.yaml](../../../openapi/openapi.yaml)
