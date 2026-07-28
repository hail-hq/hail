# AWS SES (email)

Outbound and inbound email go through [Amazon SES](https://aws.amazon.com/ses/) ([SESv2 API](https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_Operations.html)). You need an AWS account and the SES service enabled in one region. You also need IAM-role credentials (recommended for EC2/ECS/EKS deployments) or a long-lived access key.

## 1. Credentials

The Python SDK uses the standard [boto3 credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html). If you run on AWS infrastructure with an attached IAM role, leave the keys empty in `.env`. Otherwise, set:

```bash
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIA…
AWS_SECRET_ACCESS_KEY=…
```

Minimal IAM policy (one statement is sufficient for the v1 surface):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "ses:SendEmail",
        "ses:CreateEmailIdentity",
        "ses:GetEmailIdentity",
        "ses:DeleteEmailIdentity"
      ],
      "Resource": "*"
    }
  ]
}
```

## 2. Sandbox vs production

New SES accounts start in the [sandbox](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html): 200 messages/day, 1 message/second, and you can send only **to verified addresses**. When you are ready to send to arbitrary recipients, request production access from **SES → Account dashboard → Request production access**.

## 3. Why `mail.hail.so` (a subdomain), not `hail.so`

Always send transactional mail from a **dedicated subdomain**, not from your apex domain. There are two reasons:

- **Reputation isolation.** If a bounce spike or a spam-report cluster damages the sender reputation, only the subdomain takes the damage. Your apex domain (website, marketing email if any) stays clean.
- **Standard practice.** Postmark uses `mtasv.net`, and SendGrid uses `sendgrid.net`. Resend has tenants verify their own subdomains. Do not mix transactional volume with brand traffic on the apex domain — it is a known risk.

The recommended subdomain for the Hail public cloud is `mail.hail.so`. Self-hosters: select a subdomain that you own, for example `mail.<your-domain>`.

## 4. Set up the SES identity

This is operator setup, done once per deployment. Hail does not configure SES for the parent `HAIL_MAIL_BASE_DOMAIN`. That identity must exist, and be verified, before the first `POST /emails`. The operator also configures the MAIL FROM subdomain. Custom tenant domains follow a separate, fully automated flow: Hail calls `CreateEmailIdentity` **and** configures their MAIL FROM (refer to §7).

1. Open **AWS Console → SES → Verified identities → Create identity → Domain**. Enter the bare subdomain (`mail.hail.so`). Enable **DKIM** (the default; keep the bit-length at 2048).
2. SES returns three CNAMEs of the form `<token>._domainkey.mail.hail.so → <token>.dkim.amazonses.com`. **Publish all three** at your DNS provider. Wait until SES sets the status to **Verified** (usually less than 1 hour).
3. **Configure a custom MAIL FROM domain** — this step is **mandatory** for production deliverability (refer to §5 below). On the identity detail page, select **Edit MAIL FROM** and set the value to `bounces.mail.hail.so`. SES returns one MX record and one TXT record. Publish both at DNS:
   ```
   bounces.mail.hail.so  MX   10  feedback-smtp.us-east-1.amazonses.com
   bounces.mail.hail.so  TXT  "v=spf1 include:amazonses.com ~all"
   ```
4. Wait until the MAIL FROM domain status is **Success**.

> This MAIL FROM subdomain — `bounces.mail.hail.so` on the **operator parent** — is operator-managed: you configure it once, manually, in the steps above. **Custom tenant domains are different**: Hail configures their MAIL FROM automatically. `POST /email-domains` (kind=`custom`) calls `PutEmailIdentityMailFromAttributes` for `send.<domain>` and returns the MX + SPF records to publish together with the DKIM CNAMEs. `POST /email-domains/{id}/verify` then re-polls both the DKIM status and the MAIL FROM status. Refer to §7.

## 5. DMARC alignment (required for inbox delivery)

Without an aligned MAIL FROM, SES uses `<random>@amazonses.com` as the Return-Path. SPF then authenticates against `amazonses.com`, not against your domain. This breaks DMARC alignment, and the DMARC policies of recipients push your mail to spam.

With `bounces.mail.hail.so` as MAIL FROM:

- **SPF** authenticates against `bounces.mail.hail.so` → aligns with the From-domain `mail.hail.so`.
- **DKIM** signs with `mail.hail.so` → aligns.
- Both aligned → DMARC `pass` → inbox.

Publish a DMARC record. Start at `p=none` (monitor-only). Increase the policy after some weeks of clean reports:

```
_dmarc.mail.hail.so  TXT  "v=DMARC1; p=none; rua=mailto:dmarc-reports@hail.so; adkim=s; aspf=s"
```

Field summary: `p=none` reports but does not block (start here). `quarantine` moves unaligned mail to spam. `reject` drops it. `adkim=s` and `aspf=s` require strict alignment between the DKIM/SPF authentication domain and the From domain.

> **TODO(dmarc-ratchet):** Hail's public `mail.hail.so` is currently at `p=none`. After 30 or more days of clean DMARC aggregate reports (no unauthenticated mail in `rua=` feeds), step the policy to `p=quarantine`. Monitor for 30 more days, then move to `p=reject`. Self-hosters must follow the same staged rollout on their own subdomain.

## 6. Configure Hail

```bash
HAIL_MAIL_BASE_DOMAIN=mail.hail.so

# Single-variable form (self-hosters running a SINGLE org):
HAIL_MAIL_FROM=admin+selfhost@mail.hail.so

# Multi-tenant: leave HAIL_MAIL_FROM unset. The org prefix is derived
# per-org from the organization id; set only the default user prefix:
# HAIL_MAIL_DEFAULT_USER_PREFIX=admin

AWS_REGION=us-east-1
```

Hail-mail addresses always have the shape `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` — for example `alice+acme@mail.hail.so`. Hail validates both `<user>` and `<org>` against `^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$` (1–20 characters, lowercase alphanumeric plus hyphens, no leading or trailing hyphen).

Precedence at send time (highest wins):

- **User prefix:** explicit `local_prefix_user` → `HAIL_MAIL_FROM` (user part) → `HAIL_MAIL_DEFAULT_USER_PREFIX`.
- **Org prefix:** explicit `local_prefix_org` → `HAIL_MAIL_FROM` (org part, single-tenant) → derived per-org from the organization id. The org prefix is never a deploy-wide constant — that constant would make every org collide on one address.

### Self-host vs managed

**Self-hosters**: there is no console, so the env vars _are_ the configuration. Set them in `.env` once, then restart. `POST /emails` then works without a prior `POST /email-domains` — the server auto-mints a hail-mail row from the env defaults on the first send.

**Managed cloud**: the website provisions the hail-mail row of each org at signup. It calls `POST /email-domains` with prefixes derived from the org slug and the user identity. Org admins then change the visible address through the console, which writes via `PATCH /email-domains/{id}`. The env vars provide deploy-time defaults but rarely surface to tenants directly.

## 7. Custom (tenant) domains

Tenants can register their own DNS-controlled domain:

```bash
curl -X POST $HAIL_API_URL/email-domains \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"kind":"custom","domain":"acme.com"}'
```

The response returns the full DNS record set to publish:

- **three DKIM `_domainkey` CNAMEs** — `<token>._domainkey.acme.com → <token>.dkim.amazonses.com`;
- **a MAIL FROM MX + SPF TXT on `send.acme.com`** — Hail configures the custom MAIL FROM automatically, so there is no AWS-console step for the tenant:
  ```
  send.acme.com  MX   10  feedback-smtp.<region>.amazonses.com
  send.acme.com  TXT      "v=spf1 include:amazonses.com ~all"
  ```

After the tenant publishes all records, the tenant calls `POST /email-domains/{id}/verify` to re-poll SES for **both** the DKIM status and the MAIL FROM status.

```bash
hail email domain register --kind custom --domain acme.com
# → prints the DKIM CNAMEs + the send.acme.com MAIL FROM records in a copy-pastable table
hail email domain verify <id>
# → re-polls SES; flips the row to verified once the records are live
```

> Inbound on a custom domain: after the row is `verified`, enable inbound (`forward_to` and/or a webhook) to receive mail. Matching is by identity, so each receiving domain yields its own inbound row + webhook. Receiving still relies on the operator's region-wide SES receipt rule (§10).

## 8. Send

```bash
hail email send --to alice@example.com --subject "hi" --body "hello"
```

Or via HTTP:

```bash
curl -X POST $HAIL_API_URL/emails \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to": ["alice@example.com"],
    "subject": "hi from Hail",
    "body_text": "this just works"
  }'
```

The `from` field is optional. Resolution order:

1. Explicit `from` — must match a `verified` email_domain row owned by the caller's org.
2. The first verified org-owned domain (ordered by `created_at`, so the default sender stays stable when you add more domains).
3. The auto-minted hail-mail row, if `HAIL_MAIL_BASE_DOMAIN` and the prefixes are configured.

If none of these resolve, the call returns `503` with instructions to register a domain.

## 8a. Attachments

Upload a file once and attach it to as many sends as you want. The file size limit is 10MB per upload. SES caps the total message size (including all attachments) at its 10MB raw-message limit.

### Upload a file

```bash
curl -s -X POST $HAIL_API_URL/email-attachments \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -F "file=@invoice.pdf" | jq -r .id
# → "3fa85f64-5717-4562-b3fc-2c963f66afa6"
```

The response is a JSON object with an `id` field (UUID). Store this id to reference the attachment in sends.

### Attach to a send

Pass `attachment_ids` (a list of UUIDs) in the `POST /emails` payload:

```bash
curl -X POST $HAIL_API_URL/emails \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "to": ["alice@example.com"],
    "subject": "Invoice",
    "body_text": "See attached.",
    "recipient_consent": true,
    "attachment_ids": ["3fa85f64-5717-4562-b3fc-2c963f66afa6"]
  }'
```

You can attach the same uploaded file to multiple sends without a new upload. CLI shortcut — upload and attach in one step:

```bash
hail email send --to alice@example.com --subject "Invoice" --body "See attached." --attach invoice.pdf
```

### Lifecycle

Hail garbage-collects unused uploads (not attached to any send) 24 hours after upload. After you attach the file to a send, Hail retains it indefinitely. You can reuse it across as many messages as you want.

## 9. What v1 does not do

Skip these until later milestones. This list names them so that you do not use SES features that are not wired yet:

- **Templates** — the API takes raw `body_text` / `body_html`. SES templates are a v2 request.
- **Cloud-agnostic inbound** — the SMTP listener is stubbed at [`docs/setup/smtp-inbound.md`](smtp-inbound.md); inbound currently runs on AWS only.

## 10. Inbound email

To receive mail at `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>`, you need four things:

1. An MX record on `mail.hail.so` that points at SES inbound.
2. An S3 bucket that SES can write raw MIME into.
3. A SES Receipt Rule that writes the object and invokes a Lambda.
4. A small Lambda that signs the SES event and POSTs it to Hail.

A Terragrunt wrapper at `infra/terragrunt.hcl` automates provisioning
around the bare Terraform module in `infra/terraform/`. The wrapper
configures an S3-backed remote state with a DynamoDB lock table. It
pulls every input from the repo's `.env` — no parallel `tfvars` file.

### 10.1 Terragrunt apply

```bash
# .env must contain at minimum: AWS_PROFILE, AWS_REGION,
# HAIL_TERRAFORM_STATE_BUCKET, HAIL_TERRAFORM_LOCK_TABLE, HAIL_API_URL,
# HAIL_INBOUND_HMAC_SECRET (generate: openssl rand -hex 32),
# HAIL_MAIL_BASE_DOMAIN.

cd infra
terragrunt init                # Terragrunt sources .env automatically via
terragrunt plan                # `run_cmd`; no manual export step.
terragrunt apply
```

Do a one-time bootstrap per AWS account before the first `terragrunt init`:
the state bucket + lock table do not auto-create. Refer to the comment block
at the top of [`infra/terragrunt.hcl`](../../infra/terragrunt.hcl) for
the AWS CLI one-liners. Refer to [`docs/operations.md`](../operations.md) →
"Inbound email rollout → Stage 4" for the full sequence.

Outputs:

- `inbound_mx_record` — publish at DNS for `HAIL_MAIL_BASE_DOMAIN`.
- `inbound_bucket` — this is `${HAIL_MAIL_NAME_PREFIX}-mail`. Set `HAIL_MAIL_NAME_PREFIX` in the API `.env` to match the Terraform `name_prefix` var. The bucket name is not settable directly — there is no `HAIL_MAIL_BUCKET` var.
- `activate_command` — the `aws sesv2 set-active-receipt-rule-set ...` to run once.

The bare Terraform module at `infra/terraform/` is provider-vanilla. If
you prefer to skip Terragrunt, it still works with
`terraform apply -var=...`.

### 10.2 Activate the receipt rule set (manual)

SES has **one active receipt rule set per region per AWS account.** The module
creates the rule set but does **not** activate it. If an account already has
another rule set active, activation is destructive.

- **Greenfield AWS account**: run the `activate_command` output verbatim.
- **Account with existing rules**: import the existing rule set into Terraform
  state and merge Hail's rule into it. As an alternative, skip the module's rule
  resource and add Hail's rule manually via the AWS console.

### 10.3 Publish the MX record

At your DNS provider, publish what the Terraform output prints, for example:

```
mail.hail.so  MX  10  inbound-smtp.us-east-1.amazonaws.com
```

### 10.4 Configure Hail

In the API service `.env`:

```bash
HAIL_INBOUND_ENABLED=true
HAIL_MAIL_NAME_PREFIX=hail-inbound-prod         # matches Terraform `name_prefix`; bucket = ${prefix}-mail
HAIL_INBOUND_HMAC_SECRET=<same as Terraform var>
```

Restart `api`. Send a test mail to a hail-mail address and confirm:

```bash
curl "$HAIL_API_URL/emails?direction=inbound" \
  -H "Authorization: Bearer $HAIL_API_KEY"
```

Or, with the Python SDK:

```python
emails = await client.emails.list(direction="inbound")
```

If the API is down beyond the Lambda's async retries, failed deliveries land in
the `<name_prefix>-ingest-dlq` SQS queue (`ingest_dlq_url` terraform output). To
replay, re-drive each message's body at `POST /internal/ses-events`. The raw
MIME is still in S3.

## Delivery & engagement events

Outbound sends carry the SES configuration set named by
`HAIL_SES_CONFIGURATION_SET` (Terraform default: `hail-events`). SES
publishes Delivery / Bounce / Complaint / Reject / DeliveryDelay / Open /
Click events to SNS. The ingest Lambda relays them to
`POST /internal/ses-events`, which records them in `email_events`,
advances `emails.status`, and fans out webhooks.

Check a single email's timeline:

```bash
hail email events <email-id>
```

Account-level stats:

```bash
hail email stats --from 2026-06-01T00:00:00Z --bucket day
```

Notes:

- Open/Click tracking rewrites links through the default SES tracking
  domain. Hail does not yet support a custom tracking domain.
- Open counts are approximate (mail clients that proxy images inflate them).
- Hail acknowledges and drops events for mail sent outside Hail from the
  same SES account (`status: unmatched` in the API log).
- Hail re-sends forwarded inbound mail (refer to §10.5) as normal outbound.
  It carries the config set and writes a synthetic `sent` event, so it
  counts in `/emails/stats` like any other send.

### 10.5 Forwarding and webhooks

Tenants configure routing per `email_domains` row:

```bash
# Forward every inbound on the org's hail-mail address to a real inbox
curl -X PATCH $HAIL_API_URL/email-domains/$DOMAIN_ID \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"inbound_enabled":true,"forward_to":["team@acme.com"]}'

# Or POST inbound events to a webhook URL — the response carries the secret once
curl -X PATCH $HAIL_API_URL/email-domains/$DOMAIN_ID \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"inbound_enabled":true,"webhook_url":"https://hooks.acme.com/hail"}'
```

For org-wide multi-event delivery (firehose pattern):

```bash
curl -X POST $HAIL_API_URL/webhooks \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -d '{"target_url":"https://hooks.acme.com/all","event_types":["email.received","email.bounced","email.complained"]}'
```

### 10.6 Webhook secrets at rest

Hail encrypts webhook signing secrets **at rest** with a deployment-scoped
[Fernet](https://cryptography.io/en/latest/fernet/) key
([`core/hailhq/core/secret_cipher.py`](../../core/hailhq/core/secret_cipher.py)).
The worker decrypts on each delivery, so deliveries survive API restarts and
work across multi-process deployments. If `HAIL_WEBHOOK_SECRET_KEY` is unset,
webhook creation returns `500`. Generate and set the key before you enable
webhooks:

```bash
# Generate a key (run once; store in .env, never commit). Run inside the
# project venv so the `cryptography` package is on PYTHONPATH.
uv run --directory core python -c "from hailhq.core.secret_cipher import generate_key; print(generate_key())"

# In .env:
HAIL_WEBHOOK_SECRET_KEY=<output above>
```

## Reference

- [SES sending limits](https://docs.aws.amazon.com/ses/latest/dg/manage-sending-quotas.html)
- [SESv2 API](https://docs.aws.amazon.com/ses/latest/APIReference-V2/Welcome.html)
- [DKIM in SES](https://docs.aws.amazon.com/ses/latest/dg/send-email-authentication-dkim.html)
- [Custom MAIL FROM](https://docs.aws.amazon.com/ses/latest/dg/mail-from.html)
- [DMARC overview](https://dmarc.org/overview/)
- OpenAPI: [`openapi/openapi.yaml`](../../openapi/openapi.yaml) → `/emails`, `/email-domains`, `/webhooks` tags
- Code paths: [`api/hailhq/api/routes/emails.py`](../../api/hailhq/api/routes/emails.py), [`api/hailhq/api/routes/email_domains.py`](../../api/hailhq/api/routes/email_domains.py), [`api/hailhq/api/routes/webhooks.py`](../../api/hailhq/api/routes/webhooks.py), [`core/hailhq/core/providers/email/ses.py`](../../core/hailhq/core/providers/email/ses.py)
- Inbound infra: [`infra/terraform/`](../../infra/terraform/), [`infra/ses-ingest-lambda/`](../../infra/ses-ingest-lambda/)
- Design spec: [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](../superpowers/specs/2026-06-06-inbound-email-design.md)
