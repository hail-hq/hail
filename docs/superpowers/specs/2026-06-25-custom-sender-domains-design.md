# Custom sender domains — send _and_ receive on your own domain

Status: approved
Owners: r13i
Repo: `hail/` (core/api/infra) + `hail-website/` (console UI). One spec; shared model + verification worker. Implementable **outbound-first, inbound-second**.

## Goal

Let a customer send **and** receive email on their own domain instead of
`mail.hail.so`, self-serve from the console — Resend-grade: the recipient sees
their brand (no "via amazonses.com"), DNS records are copy-paste, and
verification flips on its own without a manual round-trip. Multi-tenant
integrators register many customer domains under one account and fan all inbound
into one webhook, routed per receiving domain.

This replaces the static "Coming Soon" mockup
(`hail-website/app/console/settings/CustomDomainsPanel.tsx`) with a working
panel, and closes the backend gaps surfaced in the audit below.

## The domain model

**A custom domain is one registered (sub)domain that serves _both_ directions.**
Most customers cannot point their **apex** MX at SES (it would hijack their real
corporate mail), and we want `From`/`Reply-To` to align — so the registered
identity is typically a **subdomain like `mail.acme.com`**:

- Send `From: …@mail.acme.com` (DKIM on `mail.acme.com`, MAIL FROM `send.mail.acme.com`).
- Receive at `…@mail.acme.com` (inbound MX on `mail.acme.com` itself).
- `From` and the receiving identity are the **same** domain, so replies land back
  in Hail with no separate `Reply-To` and no separate inbound subdomain.

The apex (`acme.com`) is allowed only when the customer dedicates the whole
domain to Hail. Throughout this spec `<domain>` = the registered (sub)domain.

## Starting point (audit findings — what already exists)

The **outbound happy path is already built and tested** in `hail/`:

- `EmailDomain` model with `kind='custom'` (`pending`→`verified`, `dkim_records`
  JSON) — `core/hailhq/core/models.py:364`.
- `POST /email-domains` → SES `CreateEmailIdentity` → returns 3 DKIM CNAMEs;
  `POST /{id}/verify` re-polls SES; `DELETE`; `PATCH` — `api/.../routes/email_domains.py`.
- `_resolve_sender` already selects a **verified** custom domain as the `From:`
  and matches `explicit_from` by domain suffix — `api/.../routes/emails.py:74`.
  Tested: `test_post_emails_uses_verified_custom_domain_by_default` et al.
- `SesEmailProvider` (`core/.../providers/email/ses.py`) — `create_identity`,
  `get_identity`, `delete_identity` via SESv2, mocked at the botocore boundary.

What is **missing / wrong**, and therefore in scope:

| #   | Gap                                                                                                                                               | Resolution                                                                                   |
| --- | ------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| A   | No custom MAIL FROM — Return-Path stays `amazonses.com`, recipients see "via amazonses.com".                                                      | Configure SES MAIL FROM (`send.<domain>`); surface MX + SPF records.                         |
| B   | Verification is on-demand only (`POST /{id}/verify`).                                                                                             | Background `DomainVerificationWorker` auto-polls pending rows.                               |
| C   | One org's `DELETE` calls `DeleteEmailIdentity` on a domain a **second org** may share (single AWS account) → silently breaks the other's sending. | Guard: skip the SES delete when another org still references the domain.                     |
| D   | **Inbound never matches custom domains.** Receipt rule scoped to `mail.hail.so`; `_find_domain_for_recipient` is hail-mail-only.                  | Catch-all receipt rule + a `kind='custom'` ingest branch matching on `<domain>`.             |
| E   | **Inbound attribution collapses by org**: a single message to two of an org's domains yields **one** webhook; the header carries a UUID.          | Dedup inbound rows by matched **identity**, not org; add the domain **name** to the payload. |

## Decisions (locked)

1. **One registered (sub)domain serves send + receive** (see "domain model"
   above). No separate `reply.` subdomain; no apex assumption.
2. **Custom MAIL FROM = `send.<domain>`** with SES `BehaviorOnMxFailure =
USE_DEFAULT_VALUE` — if the MX/SPF aren't published yet, sending still works
   (falls back to `amazonses.com`); no hard failure during DNS propagation.
3. **`verification_status` is DKIM-driven and gates sending.** MAIL FROM status
   is a _secondary_ badge (`mail_from_status`) that can lag without blocking
   sends. Matches Resend (domain goes green on DKIM).
4. **v1 uses SES Easy DKIM (3× CNAME).** BYODKIM (single `hail._domainkey` TXT)
   is a planned **future phase** — see Future work. The `dns_records`
   generalization below makes that swap local to `create_identity`.
5. **Reads stay direct against shared Postgres** (`pool.query`), matching the
   existing hail-mail panel (`lib/sender-domain-queries.ts`). **Mutations cross
   to the public API** as the org (`callHailApiAsOrg`) — SES side effects can
   only happen backend-side. This preserves the self-hosted ↔ managed duality.
6. **Catch-all receipt rule** (drop the `recipients` scope) over per-domain
   `UpdateReceiptRule` calls — routing already lives in the ingest layer, it is
   **automatic on register** (no manual SES step), and it sidesteps the
   200-rule / 100-recipient ceiling. ⚠️ Rewrites a **live** prod rule — see Risks.
7. **Inbound rows dedup by matched receiving identity, not by org — for
   `kind='custom'` only**, so a multi-domain message fans into one webhook **per
   receiving domain**. Hail-mail fan-out is left exactly as it is today.
8. **One spec, outbound-first.** Inbound shares the model + worker; ship sending
   first, receiving second.

## DNS records the customer publishes

For registered domain `mail.acme.com`, single SES region `<region>`:

| Group         | Type     | Host                               | Value                                     | Required     |
| ------------- | -------- | ---------------------------------- | ----------------------------------------- | ------------ |
| DKIM (verify) | CNAME ×3 | `<token>._domainkey.mail.acme.com` | `<token>.dkim.amazonses.com`              | Send         |
| MAIL FROM     | MX       | `send.mail.acme.com`               | `10 feedback-smtp.<region>.amazonses.com` | Send         |
| SPF           | TXT      | `send.mail.acme.com`               | `v=spf1 include:amazonses.com ~all`       | Send         |
| Inbound       | MX       | `mail.acme.com`                    | `10 inbound-smtp.<region>.amazonaws.com`  | Receive only |
| DMARC         | TXT      | `_dmarc.mail.acme.com`             | `v=DMARC1; p=none;`                       | Recommended  |

Note the three **distinct** endpoints — do not confuse them:

- DKIM CNAME target: `*.dkim.amazonses.com`
- MAIL FROM feedback MX: `feedback-smtp.<region>.amazonses.com`
- Inbound receiving MX: `inbound-smtp.<region>.**amazonaws.com**`

The inbound MX sits on the registered domain itself; the MAIL FROM feedback MX
sits on `send.<domain>` — different hostnames, no conflict. Receiving needs only
**domain verified + MX** (no DKIM beyond what sending already publishes).

## Backend changes (`hail/`)

### A. Custom MAIL FROM (`core/.../providers/email/ses.py`)

- Widen `DkimRecord` (`base.py`) → general `DnsRecord`:
  `type: Literal["CNAME","MX","TXT"]`, add optional `priority: int | None`.
  Keep a `DkimRecord` alias if churn is a concern.
- `create_identity(domain)`: after `CreateEmailIdentity`, call
  `put_email_identity_mail_from_attributes(EmailIdentity=domain,
MailFromDomain=f"send.{domain}", BehaviorOnMxFailure="USE_DEFAULT_VALUE")`.
  Return DKIM CNAMEs **plus** the MAIL FROM MX (`feedback-smtp.<region>...`,
  priority 10) and SPF TXT. Region from `settings.aws_region`.
- `get_identity(domain)`: already reads `MailFromAttributes.MailFromDomain`;
  also map `MailFromAttributes.MailFromDomainStatus` → `mail_from_status`.
- `ProviderIdentity`: add `mail_from_status`, return the full `dns_records` list.

### B. Auto-poll verification (`core/.../domain_verification_worker.py`, new)

- Mirror `OutboundForwardWorker`: `run_forever()` → `tick()` selects
  `kind='custom'` rows where `verification_status='pending'`, calls
  `provider.get_identity`, writes status + records + `mail_from_status`.
- Stop polling after a TTL (72h, Resend parity) → mark `failed`. `POST /{id}/verify`
  stays the manual "Verify now / Restart" trigger and resets the TTL clock.
- Wire into `api/.../main.py` lifespan behind a setting
  (`HAIL_DOMAIN_VERIFY_POLL_SECONDS`, default ~120, `0` disables), exactly like
  the webhook / outbound workers.

### C. Shared-identity delete guard (`api/.../routes/email_domains.py`)

- In `delete_email_domain`, before `provider.delete_identity(...)`: query for
  **any other org** with a row for the same `domain`. If one exists, skip the SES
  delete (drop only this org's row). Same for the MAIL FROM teardown.
- 🟡 Residual, accepted + documented: the _verify free-ride_ — if the true owner
  verifies `<domain>` in org A, a squatter row in org B reads "verified" off the
  shared SES identity. Unfixable with SES Easy DKIM (identity-level, not org).
  Low likelihood; recorded as a known limitation.

### D. Inbound on custom domains

- **Model** (`models.py` + Alembic migration):
  - rename `dkim_records` → `dns_records` (now holds all record types);
  - add `mail_from_status TEXT NULL`.
  - No `inbound_domain` column — the registered `domain` is the receiving
    identity. `inbound_enabled` / `forward_to` already exist.
- **Ingest** (`core/.../email_ingest.py`): `_find_domain_for_recipient` gains a
  `kind='custom'` branch — match the recipient's domain part against `domain`
  where `inbound_enabled AND verification_status='verified'`; **any** local-part
  matches (custom domains aren't `user+org`). Hail-mail path
  (`classify_hail_mail_recipient`) untouched and must keep passing.
- **Infra** (`infra/terraform/ses_inbound.tf`): convert
  `aws_ses_receipt_rule.main` to **catch-all** (remove
  `recipients = [var.hail_mail_base_domain]`). Lambda + S3 actions unchanged.
- **Enable flow**: enabling inbound surfaces the inbound MX. No per-domain SES
  write (catch-all handles routing); automatic the moment DNS + verification land.

### E. Inbound attribution for multi-tenant integrators

- **Dedup by matched identity, not org** — `email_ingest.py:368` currently
  dedups recipients by `organization_id` (`seen_orgs`), collapsing a message that
  hits two of an org's domains into one row/webhook. Change the dedup key to the
  matched `EmailDomain.id` so each receiving domain yields its own inbound row +
  `email.received` webhook. **Scoped to `kind='custom'`** — the hail-mail branch
  keeps its existing org-dedup. Regression test the hail-mail path is unchanged.
- **Expose the domain _name_** — today `webhook_worker.py:184` sets
  `X-Hail-Email-Domain: <EmailDomain.id UUID>`. Integrators route on the domain
  string, not a UUID. Add the matched domain **name** to the `email.received`
  payload (`webhook_fanout.py`, e.g. `email_domain: "mail.acme.com"`); keep the
  UUID header for back-compat. Optionally also emit `X-Hail-Email-Domain-Name`.

## Website changes (`hail-website/`)

### New top-level "Emails" console section

Email management currently lives inside `/console/settings`. Promote it to its
own nav entry, beside **Webhooks**.

- **Nav** (`app/console/layout.tsx`): add `<Link href="/console/emails">Emails</Link>`
  immediately after the Webhooks link.
- **New route** `app/console/emails/` (page + client) that renders the two email
  panels:
  - `EmailIdentityPanel` (hail-mail default sender) — **moved** from
    `app/console/settings/`.
  - `CustomDomainsPanel` (the new custom-domains UI below) — **moved** from
    `app/console/settings/`.
  - The data the panels need (`getEmailIdentity`, base domain, the new
    custom-domain list) loads in the new `emails/page.tsx` (move the relevant
    fetches out of `settings/page.tsx`).
- **Remove** both panels (and their now-unused loads) from `settings/page.tsx`
  and `SettingsClient.tsx`. Settings keeps Personal / Password / Sessions /
  Organization / Audit.

### Data layer

- `lib/custom-domain-queries.ts` (new, mirrors `sender-domain-queries.ts`):
  read `kind='custom'` rows from shared Postgres — `id, domain,
verification_status, mail_from_status, dns_records, inbound_enabled,
forward_to, created_at`.
- Mutations via `callHailApiAsOrg`: `POST /email-domains`, `POST /{id}/verify`,
  `DELETE /{id}`, `PATCH /{id}` (inbound toggle / forward_to).

### Server actions (`app/console/emails/actions.ts`, gated by `requireOrgAdmin`)

- `addCustomDomainAction(domain)`, `verifyCustomDomainAction(id)`,
  `deleteCustomDomainAction(id)`, `setInboundAction(id, {enabled, forwardTo})`.
- `deleteCustomDomainAction` maps backend **409 (linked emails)** to a friendly
  "This domain has sent mail and can't be removed yet" message.

### UI — replace the mockup (`CustomDomainsPanel.tsx`)

- Remove the `★ COMING SOON` pill and the `mailto:hi@hail.so` button.
- Group records like Resend: **Domain Verification (DKIM)** · **Sending (MAIL
  FROM/SPF)** · **Receiving (toggle → inbound MX + `forward_to`)**, each row with
  copy-to-clipboard, a per-group status badge, and the overall `verified /
pending / failed` state. Show DMARC as a labeled "recommended, not required".
- **Live updates**: while any domain is `pending`, `router.refresh()` on a
  ~10s interval so the poller's progress appears without a manual reload; stop
  once everything is verified/failed. Keep an explicit **"Verify now"** button.

## Docs — integrator FAQ (no code)

Add to the inbound-email docs:

- **Rate limits / latency** on `GET /emails/{id}` and
  `GET /emails/{id}/attachments/{aid}`: no rate limiting today; both are a single
  indexed DB read (+ an S3 presign for attachments).
- **Presigned-URL TTL = 300s.** Retry against the **stable**
  `…/emails/{id}/attachments/{aid}` (302) endpoint, **not** the baked `url` in the
  webhook payload (which is a pre-expiring presigned link).
- **Verify status values** = `pending` / `verified` / `failed`; no rate limit on
  `POST /{id}/verify`. With the auto-poll worker, self-serve flows rarely need to
  poll it at all.

## Testing

- **Backend**: extend `test_email_domains_api.py` / `test_emails_api.py` and
  `core/tests/providers/test_ses_email.py` (Stubber) — MAIL FROM in
  create/verify, `mail_from_status` mapping, the cross-org delete guard, the
  worker's pending→verified/`failed`-after-TTL transitions, the `kind='custom'`
  inbound match, **per-identity dedup** (multi-domain message → N rows/webhooks),
  and the domain-name payload field. **Regressions: the existing `mail.hail.so`
  inbound flow still matches under the catch-all rule, and the dedup change
  doesn't break expected hail-mail fan-out.**
- **Website** (vitest): the server actions (incl. 409 mapping), panel render
  across `pending`/`verified`/`failed`, copy buttons, inbound toggle, and the new
  `/console/emails` route + nav entry.

## Risks & confirm-during-implementation

1. 🟡 **Catch-all is a live prod migration**, not an additive resource — it drops
   the `mail.hail.so` scope on the active inbound rule. Careful cutover; the
   regression test above is the gate.
2. **Per-identity dedup is scoped to `kind='custom'`** so hail-mail fan-out is
   untouched. Regression-test the hail-mail path explicitly to prove it.
3. **`dns_records` rename** touches the model, the create/verify writers, the
   website read, and a migration — land it atomically.
4. SES **production access / sending quota** is an operational prerequisite
   (AWS support request), not code.

## Future work (explicit TODOs — out of scope here)

- **Body in the webhook**: optional `include_body` on the subscription (or always
  include `body_text`/`body_html`) to kill the `GET /emails/{id}` N+1 on inbound.
  Small, high-value fast-follow. _(Not selected for this spec; pull in on request.)_
- **Email event store + replay**: queryable email event log + webhook redelivery
  parity with calls (`/events` is call-only today; email events are transient past
  the retry window). Its own milestone/spec.
- **BYODKIM**: replace Easy DKIM's 3 CNAMEs with a single `hail._domainkey` TXT
  (Hail generates + stores the keypair, publishes the public key, calls SES with
  it). Cleaner one-record UX matching Resend. Localized to `create_identity` +
  key storage thanks to the `dns_records` generalization.
- **Open/click tracking** (Resend's `links` CNAME): SES configuration-set
  open/click tracking + per-domain tracking subdomain + event store + analytics
  UI. Its own feature/spec.
