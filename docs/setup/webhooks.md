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
- **`email.bounced`**, **`email.complained`** — subscribable today, **not yet
  emitted**. They land with SES bounce/complaint ingestion in the next
  milestone; subscribing now is safe but you won't receive them until then.

## Payload

Hail wraps every event in this envelope (assembled by `build_event_payload` in
[`webhooks.py`](../../core/hailhq/core/webhooks.py)); the `data` shape comes from
[`build_event_data`](../../core/hailhq/core/webhook_fanout.py) — that function is
the source of truth, the example below is illustrative.

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
