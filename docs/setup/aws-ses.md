# AWS SES (outbound email)

Outbound email goes through [Amazon SES](https://aws.amazon.com/ses/) ([SESv2 API](https://docs.aws.amazon.com/ses/latest/APIReference-V2/API_Operations.html)). You need an AWS account, the SES service enabled in one region, and either IAM-role credentials (recommended for EC2/ECS/EKS deployments) or a long-lived access key.

## 1. Credentials

The Python SDK uses the standard [boto3 credential chain](https://boto3.amazonaws.com/v1/documentation/api/latest/guide/credentials.html), so leave keys empty in `.env` if you run on AWS infra with an attached IAM role. Otherwise:

```bash
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIA…
AWS_SECRET_ACCESS_KEY=…
```

Minimal IAM policy (one statement is enough for the v1 surface):

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

Brand-new SES accounts are in the [sandbox](https://docs.aws.amazon.com/ses/latest/dg/request-production-access.html): 200 messages/day, 1 message/second, and you can only send **to verified addresses**. Request production access from **SES → Account dashboard → Request production access** once you're ready to send to arbitrary recipients.

## 3. Why `mail.hail.so` (a subdomain), not `hail.so`

Always send transactional mail from a **dedicated subdomain**, not your apex domain. The two reasons:

- **Reputation isolation.** If a bounce spike or spam-report cluster damages the sender's reputation, only the subdomain takes the hit. Your apex domain (website, marketing email if any) stays clean.
- **Standard practice.** Postmark uses `mtasv.net`, SendGrid `sendgrid.net`, Resend has tenants verify their own subdomains. Mixing transactional volume with brand traffic on the apex is a known footgun.

Recommended for the Hail public cloud: `mail.hail.so`. Self-hosters: pick a subdomain you own, e.g. `mail.<your-domain>`.

## 4. Set up the SES identity

Operator setup, once per deployment. Hail does not configure SES on your behalf for the parent `HAIL_MAIL_BASE_DOMAIN` — that identity has to exist (and be verified) before the first `POST /emails`, and the MAIL FROM subdomain is also operator-configured. Custom tenant domains follow a separate, fully-automated flow (Hail calls `CreateEmailIdentity` **and** configures their MAIL FROM; see §7).

1. **AWS Console → SES → Verified identities → Create identity → Domain**. Enter the bare subdomain (`mail.hail.so`). Enable **DKIM** (default; leave the bit-length at 2048).
2. SES returns three CNAMEs of the form `<token>._domainkey.mail.hail.so → <token>.dkim.amazonses.com`. **Publish all three** at your DNS provider. Wait for SES to flip status to **Verified** (usually < 1 hour).
3. **Configure a custom MAIL FROM domain** — **not optional** for production deliverability (see §5 below). On the identity detail page → **Edit MAIL FROM** → set to `bounces.mail.hail.so`. SES returns one MX and one TXT record. Publish both at DNS:
   ```
   bounces.mail.hail.so  MX   10  feedback-smtp.us-east-1.amazonses.com
   bounces.mail.hail.so  TXT  "v=spf1 include:amazonses.com ~all"
   ```
4. Wait for the MAIL FROM domain to flip to **Success**.

> This MAIL FROM subdomain — `bounces.mail.hail.so` on the **operator parent** — is operator-managed: you configure it once, by hand, in the steps above. **Custom tenant domains are different**: Hail auto-configures their MAIL FROM. `POST /email-domains` (kind=`custom`) calls `PutEmailIdentityMailFromAttributes` for `send.<domain>` and returns the MX + SPF records to publish alongside the DKIM CNAMEs; `POST /email-domains/{id}/verify` then re-polls both DKIM and MAIL FROM status. See §7.

## 5. DMARC alignment (required for inbox delivery)

Without an aligned MAIL FROM, SES uses `<random>@amazonses.com` as the Return-Path. SPF then authenticates against `amazonses.com`, not your domain — and that breaks DMARC alignment, so recipients' DMARC policies push your mail to spam.

With `bounces.mail.hail.so` as MAIL FROM:

- **SPF** authenticates against `bounces.mail.hail.so` → aligns with the From-domain `mail.hail.so`.
- **DKIM** signs with `mail.hail.so` → aligns.
- Both aligned → DMARC `pass` → inbox.

Publish a DMARC record. Start at `p=none` (monitor-only), then ratchet up after a couple of weeks of clean reports:

```
_dmarc.mail.hail.so  TXT  "v=DMARC1; p=none; rua=mailto:dmarc-reports@hail.so; adkim=s; aspf=s"
```

Field cheat-sheet: `p=none` reports but doesn't block (start here); `quarantine` shunts unaligned mail to spam; `reject` drops it. `adkim=s` and `aspf=s` require strict alignment between the DKIM/SPF authentication domain and the From domain.

> **TODO(dmarc-ratchet):** Hail's public `mail.hail.so` currently sits at `p=none`. After 30+ days of clean DMARC aggregate reports (no unauthenticated mail in `rua=` feeds), step the policy to `p=quarantine`, monitor another 30 days, then move to `p=reject`. Self-hosters should follow the same staged rollout on their own subdomain.

## 6. Configure Hail

```bash
HAIL_MAIL_BASE_DOMAIN=mail.hail.so

# Single-variable form (recommended for self-hosters):
HAIL_MAIL_FROM=admin+selfhost@mail.hail.so

# Or, alternative split form (deploy-time defaults for managed cloud):
# HAIL_MAIL_DEFAULT_USER_PREFIX=admin
# HAIL_MAIL_DEFAULT_ORG_PREFIX=selfhost

AWS_REGION=us-east-1
```

Hail-mail addresses always have the shape `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` — e.g. `alice+acme@mail.hail.so`. Both `<user>` and `<org>` are validated against `^[a-z0-9]([a-z0-9-]{0,18}[a-z0-9])?$` (1–20 chars, lowercase alphanumeric + hyphens, no leading/trailing hyphen).

Precedence at send time (highest wins):

1. Explicit `local_prefix_user` / `local_prefix_org` in the `POST /email-domains` body.
2. `HAIL_MAIL_FROM` env var, split server-side.
3. `HAIL_MAIL_DEFAULT_USER_PREFIX` + `HAIL_MAIL_DEFAULT_ORG_PREFIX` env vars.

### Self-host vs managed

**Self-hosters**: there's no console, so the env vars _are_ the configuration. Set them in `.env` once, restart, and `POST /emails` works without any prior `POST /email-domains` — the server auto-mints a hail-mail row from the env defaults on first send.

**Managed cloud**: the website provisions each org's hail-mail row at signup (calling `POST /email-domains` with prefixes derived from the org slug + user identity). Org admins then change the visible address through the console, which writes via `PATCH /email-domains/{id}`. The env vars provide deploy-time defaults but rarely surface to tenants directly.

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

After publishing all of them, the tenant calls `POST /email-domains/{id}/verify` to re-poll SES for **both** DKIM and MAIL FROM status.

```bash
hail email domain register --kind custom --domain acme.com
# → prints the DKIM CNAMEs + the send.acme.com MAIL FROM records in a copy-pastable table
hail email domain verify <id>
# → re-polls SES; flips the row to verified once the records are live
```

> Inbound on a custom domain: once the row is `verified`, enabling inbound (`forward_to` and/or a webhook) lets it receive — matched by identity, so each receiving domain yields its own inbound row + webhook. Receiving still relies on the operator's region-wide SES receipt rule (§10).

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
2. First verified org-owned domain (ordered by `created_at`, so the default sender stays stable as more get added).
3. Auto-minted hail-mail row, if `HAIL_MAIL_BASE_DOMAIN` and prefixes are configured.

If none of those resolve, the call returns `503` pointing at how to register a domain.

## 9. What v1 doesn't do

Skip these until later milestones — they're called out so you don't reach for SES features that aren't wired yet:

- **Templates** — the API takes raw `body_text` / `body_html`. SES templates are a v2 ask.
- **Attachments on outbound** — `Content.Simple` only; raw MIME is not exposed. Inbound attachments _are_ stored (see §10).
- **Cloud-agnostic inbound** — the SMTP listener is stubbed at [`docs/setup/smtp-inbound.md`](smtp-inbound.md); inbound currently runs on AWS only.

## 10. Inbound email

Receiving mail at `<user>+<org>@<HAIL_MAIL_BASE_DOMAIN>` requires four things:

1. An MX record on `mail.hail.so` pointing at SES inbound.
2. An S3 bucket SES can write raw MIME into.
3. A SES Receipt Rule that writes the object and invokes a Lambda.
4. A small Lambda that signs the SES event and POSTs it to Hail.

Provisioning is automated by a Terragrunt wrapper at `infra/terragrunt.hcl`
around the bare Terraform module in `infra/terraform/`. The wrapper
configures an S3-backed remote state with a DynamoDB lock table and
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

One-time bootstrap per AWS account before the first `terragrunt init`:
the state bucket + lock table don't auto-create. See the comment block
at the top of [`infra/terragrunt.hcl`](../../infra/terragrunt.hcl) for
the AWS CLI one-liners, and [`docs/operations.md`](../operations.md) →
"Inbound email rollout → Stage 4" for the full sequence.

Outputs:

- `inbound_mx_record` — publish at DNS for `HAIL_MAIL_BASE_DOMAIN`.
- `inbound_bucket` — set as `HAIL_INBOUND_BUCKET` in the API `.env`.
- `activate_command` — the `aws sesv2 set-active-receipt-rule-set ...` to run once.

The bare Terraform module at `infra/terraform/` is provider-vanilla and
still works with `terraform apply -var=...` if you'd prefer to skip
Terragrunt.

### 10.2 Activate the receipt rule set (manual)

SES has **one active receipt rule set per region per AWS account.** The module
creates the rule set but does **not** activate it (activation is destructive when
an account already has another rule set active).

- **Greenfield AWS account**: run the `activate_command` output verbatim.
- **Account with existing rules**: import the existing rule set into Terraform
  state and merge Hail's rule into it, or skip the module's rule resource and
  add Hail's rule manually via the AWS console.

### 10.3 Publish the MX record

At your DNS provider, publish what the Terraform output prints, e.g.:

```
mail.hail.so  MX  10  inbound-smtp.us-east-1.amazonaws.com
```

### 10.4 Configure Hail

In the API service `.env`:

```bash
HAIL_INBOUND_ENABLED=true
HAIL_INBOUND_BUCKET=hail-inbound-prod-raw      # from terraform output
HAIL_INBOUND_HMAC_SECRET=<same as Terraform var>
```

Restart `api`. Send a test mail to a hail-mail address and confirm:

```bash
curl "$HAIL_API_URL/emails?direction=inbound" \
  -H "Authorization: Bearer $HAIL_API_KEY"
```

Or, using the Python SDK:

```python
emails = await client.emails.list(direction="inbound")
```

If the API is down beyond the Lambda's async retries, failed deliveries land in
the `<name_prefix>-ingest-dlq` SQS queue (`ingest_dlq_url` terraform output) —
replay by re-driving each message's body at `POST /internal/ses-events`; the raw
MIME is still in S3.

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
hail webhooks create \
  --url https://hooks.acme.com/all \
  --events email.received,email.bounced,email.complained
```

### 10.6 Webhook secrets at rest

Webhook signing secrets are **encrypted at rest** with a deployment-scoped
[Fernet](https://cryptography.io/en/latest/fernet/) key
([`core/hailhq/core/secret_cipher.py`](../../core/hailhq/core/secret_cipher.py)).
The worker decrypts on each delivery, so deliveries survive API restarts and
work across multi-process deployments. If `HAIL_WEBHOOK_SECRET_KEY` is unset,
webhook creation returns `500` — generate and set it before enabling webhooks:

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
