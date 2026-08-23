# Webhooks

When a subscribed event occurs — inbound mail or SMS, a delivery report, a
call outcome — Hail `POST`s a signed JSON event to your URL. Verify the
`X-Hail-Signature` header against the once-shown secret. Return any `2xx`,
and you are done. Hail retries a non-2xx response (or a timeout) on a fixed
ladder.

## Verify the signature (Python)

The signature is HMAC-SHA256 over `f"{t}.{body}"` — the timestamp from the
header, a literal `.`, then the **raw request bytes**. This is a runnable
example. The `assert` passes (the real signer in
[`core/hailhq/core/webhooks.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhooks.py) produced
the fixture):

```python
import hashlib, hmac

def verify(secret: str, signature_header: str, raw_body: bytes) -> bool:
    # signature_header looks like "t=1700000000,v1=<hex>"
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    t, v1 = parts["t"], parts["v1"]
    mac = hmac.new(secret.encode(), f"{t}.".encode() + raw_body, hashlib.sha256)
    return hmac.compare_digest(mac.hexdigest(), v1)

# Worked example — prints True:
assert verify(
    "whsec_example",
    "t=1700000000,v1=72f7940d13ca8528cf655a04e82dabfe90a531e239241fda9bdff72980d33a4a",
    b'{"id":"evt_123","type":"email.received","data":{"id":"em_1"}}',
)
```

## Verify the signature (Node)

Hash the **raw request body bytes**, not a re-serialized object — JSON
key order and whitespace would differ and the HMAC would not match.

```js
import crypto from "node:crypto";

function verify(secret, signatureHeader, rawBody) {
  // rawBody is a Buffer of the exact bytes Hail sent.
  const parts = Object.fromEntries(
    signatureHeader.split(",").map((p) => p.split(/=(.*)/s).slice(0, 2)),
  );
  const mac = crypto
    .createHmac("sha256", secret)
    .update(`${parts.t}.`)
    .update(rawBody)
    .digest("hex");
  return crypto.timingSafeEqual(Buffer.from(mac), Buffer.from(parts.v1));
}
```

In Express, capture the raw bytes with `express.raw({ type: "application/json" })`.
In Next.js route handlers, use `await req.text()` / `req.arrayBuffer()` and pass
those bytes, not `await req.json()`.

## Headers

Every delivery carries these headers (refer to
[`core/hailhq/core/webhook_worker.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhook_worker.py)):

| Header                | Meaning                                                                                                                     |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `X-Hail-Signature`    | `t=<unix>,v1=<hex hmac_sha256>` — verify this.                                                                              |
| `X-Hail-Event`        | Event type, for example `email.received`.                                                                                   |
| `X-Hail-Delivery`     | Unique delivery id (stable across retries; use to dedupe).                                                                  |
| `X-Hail-Subscription` | Always present — identifies the subscription that produced this delivery.                                                   |
| `X-Hail-Email-Domain` | Informational: the source email domain for inbound events (when known). Branch on this to route per-domain in your handler. |

## Event types

The full set is the `WebhookEventType` enum in
[`core/hailhq/core/schemas.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/schemas.py):

- **`email.received`** — a message arrived, and Hail accepted it.
- **`email.received.suppressed`** — a message arrived, but Hail held back
  fan-out/forwarding. `data.reason` is one of `forward_loop`, `forward_rate_limit`,
  `inbound_rate_limit`, `insufficient_funds`. One event fires _per reason_
  (a single message can produce more than one suppressed event).
- **`email.delivered`** — SES accepted the message for delivery.
- **`email.delivery_delayed`** — SES reports a transient delay.
- **`email.bounced`** — the recipient mail server rejected the message (permanent or soft bounce).
- **`email.complained`** — the recipient marked the message as spam.
- **`email.opened`** — the recipient opened the message (image tracked, approximate).
- **`email.clicked`** — the recipient clicked a tracked link.
- **`email.send_failed`** — an outbound email failed to send.
- **`sms.received`** — an inbound SMS arrived, and Hail accepted it. Hail delivers
  it through the same signed, retried webhook worker as the email events
  (`X-Hail-Signature`, `X-Hail-Event`, `X-Hail-Delivery`). Hail omits the
  `X-Hail-Email-Domain` header.
- **`sms.delivered`** — the carrier confirmed delivery (requires Twilio delivery receipts).
- **`sms.undelivered`** — the carrier reported the message was not delivered.
- **`sms.failed`** — the send failed (transport error or carrier rejection).

**Call lifecycle** — covers `answered`, `completed`, `failed`, `busy`, `no_answer`
only (no `ringing` or `canceled` events; no data source):

- **`call.answered`** — the callee picked up (call entered in-progress).
- **`call.completed`** — the call ended normally.
- **`call.failed`** — the call failed (setup error, trunk/media failure, or force-closed).
- **`call.busy`** — the callee was busy or rejected the call.
- **`call.no_answer`** — the callee did not answer.

## Payload

Hail wraps every event in this envelope (`build_event_payload` in
[`webhooks.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhooks.py) assembles it). The `data`
shape comes from the event type. Inbound events use [`build_event_data`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhook_fanout.py) for `email.received*`. Delivery events use [`build_delivery_event_data`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/email_delivery_events.py) for the lifecycle events.

**Inbound example** (`email.received`):

```json
{
  "id": "9f2c…",
  "type": "email.received",
  "api_version": "2026-06-06",
  "created_at": "2026-06-14T12:00:00+00:00",
  "organization_id": "org-uuid",
  "data": {
    "id": "em-uuid",
    "direction": "inbound",
    "from_address": "sender@example.com",
    "to_addresses": ["agent@yourorg.hail.so"],
    "subject": "Re: invoice",
    "message_id": "<abc@example.com>",
    "in_reply_to": "<def@yourorg.hail.so>",
    "spam_verdict": "PASS",
    "virus_verdict": "PASS",
    "spf_verdict": "PASS",
    "dkim_verdict": "PASS",
    "dmarc_verdict": "PASS",
    "raw_url": "https://api.hail.so/v1/emails/em-uuid/raw",
    "attachments": [
      {
        "id": "att-uuid",
        "filename": "invoice.pdf",
        "content_type": "application/pdf",
        "size_bytes": 12345,
        "url": "https://…"
      }
    ]
  }
}
```

**Inbound example** (`sms.received`):

```json
{
  "id": "7a1b…",
  "type": "sms.received",
  "api_version": "2026-06-06",
  "created_at": "2026-07-10T12:00:00+00:00",
  "organization_id": "org-uuid",
  "data": {
    "id": "sms-uuid",
    "from": "+14155551234",
    "to": "+14155559999",
    "body": "hello back"
  }
}
```

`raw_url` and each attachment `url` are Hail API endpoints that 302-redirect to a
presigned S3 URL on access. `email.received.suppressed` carries a trimmed `data`
(`id`, `direction`, `from_address`, `to_addresses`, `subject`, `message_id`,
`reason`) — no verdicts, `raw_url`, or attachments.

**Outbound delivery example** (`email.bounced`):

```json
{
  "id": "9f2c…",
  "type": "email.bounced",
  "api_version": "2026-06-06",
  "created_at": "2026-07-02T12:00:05+00:00",
  "organization_id": "org-uuid",
  "data": {
    "id": "em-uuid",
    "kind": "bounced",
    "occurred_at": "2026-07-01T12:00:05+00:00",
    "from_address": "noreply@acme.com",
    "to_addresses": ["bob@example.com"],
    "subject": "Welcome",
    "detail": {
      "hard": true,
      "bounce_type": "Permanent",
      "bounce_sub_type": "General",
      "recipients": ["bob@example.com"],
      "diagnostic_code": "smtp; 550 5.1.1 user unknown"
    }
  }
}
```

The `detail` field varies by event type. `bounced` and `complained` carry SES metadata. `delivered` and `delivery_delayed` carry SES status details. `opened` and `clicked` carry `ip_address` and `user_agent`; `clicked` adds `link` (the event time is the sibling `occurred_at` field). For `bounced`, the provider-neutral `hard` flag distinguishes hard bounces from soft bounces. Only hard bounces move the email to `status=bounced` and count toward `bounced_hard` in `GET /emails/stats`.

## Retries

Hail retries a delivery that does not get a `2xx` on this fixed ladder
(`RETRY_SCHEDULE_SECONDS` in [`webhooks.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhooks.py)):

```
0s → 30s → 2m → 10m → 1h → 6h → 24h
```

After the 7th attempt fails, Hail marks the delivery **dead**. When an org-wide
subscription accrues **50 consecutive dead** deliveries, it auto-disables. To
re-enable it, set its status back to `active`. Replay a single delivery from the
console or through the API:

```bash
curl -X POST "$HAIL_API_URL/v1/webhooks/<subscription-id>/deliveries/<delivery-id>/redeliver" \
  -H "Authorization: Bearer $HAIL_API_KEY"
```

## Subscribe

Create a subscription. The response returns the signing secret **once**, at create:

```bash
POST /webhooks   {"target_url": "https://example.com/hooks/hail",
                  "event_types": ["email.received", "email.received.suppressed"]}
```

The `event_types` enum and request/response schemas are in
[`openapi/openapi.yaml`](https://github.com/hail-hq/hail/blob/main/openapi/openapi.yaml) (`WebhookSubscriptionCreate`).
The CLI has no `webhooks` command group — for the full endpoint list, refer to
[the CLI reference](./cli.md#webhooks).
