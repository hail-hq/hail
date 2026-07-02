# Webhooks

When mail arrives for an address you've configured, Hail `POST`s a signed JSON
event to your URL. Verify the `X-Hail-Signature` header against the once-shown
secret, return any `2xx`, and you're done. A non-2xx (or a timeout) is retried
on a fixed ladder.

## Verify the signature (Python)

The signature is HMAC-SHA256 over `f"{t}.{body}"` — the timestamp from the
header, a literal `.`, then the **raw request bytes**. This is a runnable
example; the `assert` passes (the fixture was produced by the real signer in
[`core/hailhq/core/webhooks.py`](../../core/hailhq/core/webhooks.py)):

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

In Express, capture raw bytes with `express.raw({ type: "application/json" })`;
in Next.js route handlers use `await req.text()` / `req.arrayBuffer()` and pass
those bytes, not `await req.json()`.

## Headers

Every delivery carries these (see
[`core/hailhq/core/webhook_worker.py`](../../core/hailhq/core/webhook_worker.py)):

| Header                | Meaning                                                                                                                     |
| --------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `X-Hail-Signature`    | `t=<unix>,v1=<hex hmac_sha256>` — verify this.                                                                              |
| `X-Hail-Event`        | Event type, e.g. `email.received`.                                                                                          |
| `X-Hail-Delivery`     | Unique delivery id (stable across retries; use to dedupe).                                                                  |
| `X-Hail-Subscription` | Always present — identifies the subscription that produced this delivery.                                                   |
| `X-Hail-Email-Domain` | Informational: the source email domain for inbound events (when known). Branch on this to route per-domain in your handler. |

## Event types

The full set is the `WebhookEventType` enum in
[`core/hailhq/core/schemas.py`](../../core/hailhq/core/schemas.py):

- **`email.received`** — a message arrived and was accepted.
- **`email.received.suppressed`** — a message arrived but fan-out/forwarding was
  held back. `data.reason` is one of `forward_loop`, `forward_rate_limit`,
  `inbound_rate_limit`, `insufficient_funds`. One event fires _per reason_
  (a single message can produce more than one suppressed event).
- **`email.delivered`** — SES accepted the message for delivery.
- **`email.delivery_delayed`** — SES reports a transient delay.
- **`email.bounced`** — the recipient mail server rejected the message (permanent or soft bounce).
- **`email.complained`** — the recipient marked the message as spam.
- **`email.opened`** — the recipient opened the message (image tracked, approximate).
- **`email.clicked`** — the recipient clicked a tracked link.

## Payload

Hail wraps every event in this envelope (assembled by `build_event_payload` in
[`webhooks.py`](../../core/hailhq/core/webhooks.py)); the `data` shape comes from
the event type. Inbound events use [`build_event_data`](../../core/hailhq/core/webhook_fanout.py) for `email.received*`; delivery events use [`build_delivery_event_data`](../../core/hailhq/core/email_delivery_events.py) for the lifecycle events.

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
    "raw_url": "https://api.hail.so/emails/em-uuid/raw",
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

The `detail` field varies by event type: `bounced` and `complained` carry SES metadata; `delivered` and `delivery_delayed` carry SES status details; `opened` and `clicked` carry `ip_address` and `user_agent`, with `clicked` adding `link` (the event time is the sibling `occurred_at` field). For `bounced`, the provider-neutral `hard` flag distinguishes hard from soft bounces — only hard bounces move the email to `status=bounced` and count toward `bounced_hard` in `GET /emails/stats`.

## Retries

A delivery that doesn't get a `2xx` is retried on this fixed ladder
(`RETRY_SCHEDULE_SECONDS` in [`webhooks.py`](../../core/hailhq/core/webhooks.py)):

```
0s → 30s → 2m → 10m → 1h → 6h → 24h
```

After the 7th attempt fails the delivery is marked **dead**. When an org-wide
subscription accrues **50 consecutive dead** deliveries it auto-disables; re-enable
it by setting its status back to `active`. Replay a single delivery from the
console or with the CLI:

```bash
hail webhooks redeliver <subscription-id> <delivery-id>
```

## Subscribe

Create a subscription — the signing secret is returned **once** at create:

```bash
POST /webhooks   {"target_url": "https://example.com/hooks/hail",
                  "event_types": ["email.received", "email.received.suppressed"]}
```

CLI equivalent: `hail webhooks create`.

The `event_types` enum and request/response schemas are in
[`openapi/openapi.yaml`](../../openapi/openapi.yaml) (`WebhookSubscriptionCreate`).
CLI equivalents: `hail webhooks create`, `list`, `deliveries`, `redeliver` — see
[the CLI reference](../cli.md).
