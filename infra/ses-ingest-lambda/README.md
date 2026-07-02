# ses-ingest-lambda

Bridges SES Receipt Rule notifications into Hail's HMAC-signed
`/internal/ses-events` endpoint. Pure stdlib (Python 3.12). Deployed
by the Terraform module in [`../terraform/`](../terraform/).

## Environment

| Variable                   | Description                                       |
| -------------------------- | ------------------------------------------------- |
| `HAIL_API_URL`             | Base URL of the Hail API (no trailing slash).     |
| `HAIL_INBOUND_BUCKET`      | S3 bucket SES is configured to write raw MIME to. |
| `HAIL_INBOUND_HMAC_SECRET` | Shared secret with the API.                       |

## Test

```bash
cd infra/ses-ingest-lambda
python -m pytest test_handler.py -v
```

## Design

The Lambda receives the standard SES event (envelope + receipt verdicts

- S3 location), repackages it into a small JSON body, signs it with
  HMAC-SHA256, and POSTs to `/internal/ses-events`. The API fetches the
  raw MIME from S3 itself — Lambda only carries the pointer.

See [`docs/superpowers/specs/2026-06-06-inbound-email-design.md`](../../docs/superpowers/specs/2026-06-06-inbound-email-design.md) §1.

## Delivery events

`/internal/ses-events` also accepts a second, HMAC-signed envelope shape —
`{"type": "delivery_event", "event": {<raw SES event>}}` — carrying SES
configuration-set delivery/engagement notifications (Delivery, Bounce,
Complaint, Reject, DeliveryDelay, Open, Click) straight from SNS, bypassing
this Lambda's S3-pointer flow entirely. It works independently of
`HAIL_INBOUND_ENABLED`, so a deployment can track delivery status without
receiving inbound mail.
