# Email delivery

Send an email, then read what happened to it:

```bash
hail email send --to ada@example.com --subject "Hi" --body "Hello" --recipient-consent
hail email events <email-id>
```

Same send over HTTP ([OpenAPI: `POST /v1/emails`](https://github.com/hail-hq/hail/blob/main/openapi/openapi.yaml)):

```bash
curl -X POST "$HAIL_API_URL/v1/emails" \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"to": ["ada@example.com"], "subject": "Hi", "body_text": "Hello", "recipient_consent": true}'
```

## What Hail does when you send

These apply to mail you send with `POST /v1/emails` or through an agent. Forwarded mail and Hail's own notices follow other rules ([architecture](./architecture.md#bounces-and-complaints)).

- **Your domain signs the mail.** A custom domain gets DKIM and a MAIL FROM on `send.<domain>`, so SPF and DKIM align with your `From`. `hail email domain dns-check <id>` checks DKIM, MAIL FROM and DMARC, and gives a DMARC record to publish if you have none.
- **One-click unsubscribe.** An email with exactly one recipient carries `List-Unsubscribe` and `List-Unsubscribe-Post` (RFC 8058). The link works for one year. A click on it, or the Unsubscribe button in Gmail or Yahoo, adds the address to your suppression list. An email with two or more recipients carries no unsubscribe header.
- **Hard bounces and spam complaints stop future sends.** The addresses named in the bounce or complaint are added to your suppression list. A soft bounce (mailbox full, server busy) is recorded as an event only.
- **Send limits.** Each organization has an hourly and a daily send cap. Over the cap, `POST /v1/emails` returns `403`.

The suppression list belongs to your organization. Another organization on Hail can still send to that address.

## Blocked sends

A send to a suppressed address returns `403` with the reason:

```json
{ "detail": "recipient 'bob@example.com' is suppressed (hard_bounce)" }
```

Reasons: `recipient_unsubscribed`, `hard_bounce`, `spam_complaint`. One suppressed recipient in `to`, `cc` or `bcc` blocks the whole send.

List your suppressions, and remove an address:

```bash
hail email suppressions list
hail email suppressions delete bob@example.com
```

## Events and stats

Each email has a timeline of events: `sent`, `delivered`, `delivery_delayed`, `bounced`, `complained`, `rejected`, `opened`, `clicked`. Open and click tracking needs `body_html`. Get events pushed to you with a [webhook](./webhooks.md) (`email.delivered`, `email.bounced`, `email.complained`, ...).

Totals and rates for a time range:

```bash
hail email stats
curl "$HAIL_API_URL/v1/emails/stats?from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z" \
  -H "Authorization: Bearer $HAIL_API_KEY"
```

Code: [`email_delivery_events.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/email_delivery_events.py) (bounces, complaints), [`unsubscribe.py`](https://github.com/hail-hq/hail/blob/main/api/hailhq/api/routes/unsubscribe.py), [`compliance_gate.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/compliance_gate.py) (suppression and limits).
