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

Operator setup, once per deployment. Hail does not configure SES on your behalf for the parent `HAIL_MAIL_BASE_DOMAIN` — that identity has to exist (and be verified) before the first `POST /emails`, and the MAIL FROM subdomain is also operator-configured. Custom tenant domains follow a separate flow (Hail calls `CreateEmailIdentity` for those; see §7).

1. **AWS Console → SES → Verified identities → Create identity → Domain**. Enter the bare subdomain (`mail.hail.so`). Enable **DKIM** (default; leave the bit-length at 2048).
2. SES returns three CNAMEs of the form `<token>._domainkey.mail.hail.so → <token>.dkim.amazonses.com`. **Publish all three** at your DNS provider. Wait for SES to flip status to **Verified** (usually < 1 hour).
3. **Configure a custom MAIL FROM domain** — **not optional** for production deliverability (see §5 below). On the identity detail page → **Edit MAIL FROM** → set to `bounces.mail.hail.so`. SES returns one MX and one TXT record. Publish both at DNS:
   ```
   bounces.mail.hail.so  MX   10  feedback-smtp.us-east-1.amazonses.com
   bounces.mail.hail.so  TXT  "v=spf1 include:amazonses.com ~all"
   ```
4. Wait for the MAIL FROM domain to flip to **Success**.

> The MAIL FROM subdomain is **operator-managed**, not provisioned by Hail. Hail's `POST /sender-domains` (kind=`custom`) doesn't call `PutEmailIdentityMailFromAttributes`; if a tenant needs a custom MAIL FROM on their own domain they configure it in the AWS console and `POST /sender-domains/{id}/verify` to pick up the value.

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

1. Explicit `local_prefix_user` / `local_prefix_org` in the `POST /sender-domains` body.
2. `HAIL_MAIL_FROM` env var, split server-side.
3. `HAIL_MAIL_DEFAULT_USER_PREFIX` + `HAIL_MAIL_DEFAULT_ORG_PREFIX` env vars.

### Self-host vs managed

**Self-hosters**: there's no console, so the env vars _are_ the configuration. Set them in `.env` once, restart, and `POST /emails` works without any prior `POST /sender-domains` — the server auto-mints a hail-mail row from the env defaults on first send.

**Managed cloud**: the website provisions each org's hail-mail row at signup (calling `POST /sender-domains` with prefixes derived from the org slug + user identity). Org admins then change the visible address through the console, which writes via `PATCH /sender-domains/{id}`. The env vars provide deploy-time defaults but rarely surface to tenants directly.

## 7. Custom (tenant) domains

Tenants can register their own DNS-controlled domain:

```bash
curl -X POST $HAIL_API_URL/sender-domains \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"kind":"custom","domain":"acme.com"}'
```

The response includes three `_domainkey` CNAMEs to publish at DNS. After publishing, the tenant calls `POST /sender-domains/{id}/verify` to re-poll SES.

```bash
hail email sender-domain register --kind custom --domain acme.com
# → prints DKIM CNAMEs in a copy-pastable table
hail email sender-domain verify <id>
# → re-polls SES; flips row to verified once CNAMEs are live
```

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

1. Explicit `from` — must match a `verified` sender_domain row owned by the caller's org.
2. First verified org-owned domain (ordered by `created_at`, so the default sender stays stable as more get added).
3. Auto-minted hail-mail row, if `HAIL_MAIL_BASE_DOMAIN` and prefixes are configured.

If none of those resolve, the call returns `503` pointing at how to register a domain.

## 9. What v1 doesn't do

Skip these until later milestones — they're called out so you don't reach for SES features that aren't wired yet:

- **Inbound email** (v1.5) — no SES Receipt Rules, no S3 ingest, no MIME parsing.
- **Bounce / complaint webhooks** (v1.5) — `emails.status` ships as `sent`/`failed`; `bounced` and `complained` are reserved values populated by the inbound milestone.
- **Templates** — the API takes raw `body_text` / `body_html`. SES templates are a v2 ask.
- **Attachments** — `Content.Simple` only; raw MIME is not exposed.

## Reference

- [SES sending limits](https://docs.aws.amazon.com/ses/latest/dg/manage-sending-quotas.html)
- [SESv2 API](https://docs.aws.amazon.com/ses/latest/APIReference-V2/Welcome.html)
- [DKIM in SES](https://docs.aws.amazon.com/ses/latest/dg/send-email-authentication-dkim.html)
- [Custom MAIL FROM](https://docs.aws.amazon.com/ses/latest/dg/mail-from.html)
- [DMARC overview](https://dmarc.org/overview/)
- OpenAPI: [`openapi/openapi.yaml`](../../openapi/openapi.yaml) → `/emails`, `/sender-domains` tags
- Code paths: [`api/hailhq/api/routes/emails.py`](../../api/hailhq/api/routes/emails.py), [`api/hailhq/api/routes/sender_domains.py`](../../api/hailhq/api/routes/sender_domains.py), [`core/hailhq/core/providers/email/ses.py`](../../core/hailhq/core/providers/email/ses.py)
