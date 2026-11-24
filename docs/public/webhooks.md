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

For a receiver that requires its own credentials, send `authorization` when
creating or updating a subscription:

```bash
curl https://api.hail.so/v1/webhooks \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"target_url":"https://receiver.example.com/hooks","event_types":["call.completed"],"authorization":"Bearer YOUR_RECEIVER_KEY"}'
```

Hail encrypts this header with the webhook secret key. Responses return only
`has_authorization`, never the value. It is separate from Hail's signing secret.
Use one line of printable ASCII, up to 2000 characters. On PATCH, omit the field
or send `null` to keep it; send `""` to remove it. When changing a target URL
with a saved header, provide the replacement or explicitly remove it. Redirects
are not followed. Receiver responses redact the saved header and its credential.

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

Read supported event names, labels, and channels from the public catalog:

```bash
curl https://api.hail.so/v1/webhooks/event-types
```

The response contains an `items` array. Use each item's `type` in a subscription's `event_types`.
This endpoint needs no API key. Creating or changing a subscription still requires authentication.

The catalog and subscription validation share the `WebhookEventType` registry in
[`core/hailhq/core/webhook_events.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhook_events.py):

- **`email.received`** — a message arrived, and Hail accepted it.
- **`email.received.suppressed`** — a message arrived, but Hail held back
  fan-out/forwarding. `data.reason` is one of `forward_loop`, `forward_rate_limit`,
  `inbound_rate_limit`, `insufficient_funds`. One event fires _per reason_
  (a single message can produce more than one suppressed event).
  `insufficient_funds` can also fire later, when a queued forward is about to
  be sent and the balance has run out.
- **`email.delivered`** — SES accepted the message for delivery.
- **`email.delivery_delayed`** — SES reports a transient delay.
- **`email.bounced`** — the recipient mail server rejected the message (permanent or soft bounce).
- **`email.complained`** — the recipient marked the message as spam.
- **`email.opened`** — the recipient opened the message (image tracked, approximate).
  Only fires for emails sent with an HTML body; plain-text-only emails are never
  tracked for opens.
- **`email.clicked`** — the recipient clicked a tracked link. Only fires for
  emails sent with an HTML body; plain-text-only emails are never tracked for
  clicks.
- **`email.send_failed`** — an outbound email failed to send.
- **`sms.received`** — an inbound SMS arrived, and Hail accepted it. Hail delivers
  it through the same signed, retried webhook worker as the email events
  (`X-Hail-Signature`, `X-Hail-Event`, `X-Hail-Delivery`). Hail omits the
  `X-Hail-Email-Domain` header.
- **`sms.delivered`** — the carrier confirmed delivery (requires Twilio delivery receipts).
- **`sms.undelivered`** — the carrier reported the message was not delivered. `data` may include `error_code`, the carrier's error code.
- **`sms.failed`** — the send failed (transport error or carrier rejection). `data` may include `error_code`, the carrier's error code.

**Call lifecycle** — `received` (inbound only), `answered`, `completed`,
`failed`, `busy`, `no_answer`, `transferred`, `summarized` (there is no `canceled` event; it has no data source):

- **`call.received`** — an inbound call reached one of your numbers and an
  agent is about to answer ([Agents](agents.md)). Fired before the answer.
- **`call.answered`** — the other side is on the line (call entered in-progress).
- **`call.completed`** — the call ended normally.
- **`call.failed`** — the call failed (setup error, trunk/media failure,
  force-closed, or an inbound call Hail refused: `end_reason` `no_agent` or
  `insufficient_funds`).
- **`call.busy`** — the callee was busy or rejected the call.
- **`call.no_answer`** — the callee did not answer.
- **`call.transferred`** — the agent handed the live call to a person
  ([Agents](agents.md#hand-over-to-a-person)). Sent once, when someone on
  that person's line has spoken and stayed on for 10 seconds. `data` is the call object plus `transfer`:
  `{"contact_id": "…", "contact_name": "Sam"}`. `contact_id` is the id
  `GET /contacts` returns: a contact's uuid, or `member:<user id>` for a
  team member. Never a phone number.
- **`call.summarized`** — the call's transcript and a short AI summary, sent
  once after the call ends ([Call summarized](#call-summarized)).

Every `call.*` payload's `data` carries `id`, `status`, `direction`
(`inbound` or `outbound`), `from`, `to`, `agent_id` (null for calls placed
with an inline prompt) and, once terminal, `end_reason`
([`call_event_data`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/webhook_fanout.py)):

Failed call responses and `call.failed` events also carry `failure_message`.
For a confirmed geographic-permission block, it explains that calling the
destination is disabled. Raw carrier error text is never returned.

```json
{
  "type": "call.received",
  "data": {
    "id": "0d9c…",
    "status": "ringing",
    "direction": "inbound",
    "from": "+33612345678",
    "to": "+14155550100",
    "agent_id": "a1b2…"
  }
}
```

## Call summarized

Subscribe to `call.summarized` to get what was said on each call and a short
summary, about 10 seconds after the call ends:

```bash
curl -X POST "$HAIL_API_URL/v1/webhooks" \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"target_url": "https://example.com/hooks/hail", "event_types": ["call.summarized"]}'
```

```json
{
  "type": "call.summarized",
  "data": {
    "id": "0d9c…",
    "status": "completed",
    "direction": "inbound",
    "from": "+33612345678",
    "to": "+14155550100",
    "agent_id": "a1b2…",
    "end_reason": "normal_hangup",
    "duration_seconds": 74,
    "transcript": [
      {
        "role": "assistant",
        "text": "Hi, this is an AI assistant answering on behalf of Acme.",
        "at": "2026-11-17T12:00:02Z"
      },
      {
        "role": "user",
        "text": "Hi, I'd like to move my Tuesday appointment.",
        "at": "2026-11-17T12:00:06Z"
      }
    ],
    "summary": "The caller asked to move their Tuesday appointment. The agent moved it to Thursday at 10am."
  }
}
```

- `data` is built by `call_summarized_data` in
  [`call_analysis.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/call_analysis.py).
  It has the `call.*` fields above, plus `duration_seconds`, `transcript` and `summary`.
- `transcript` holds the same turns as `GET /v1/calls/{id}`, in the order
  they were said. `at` is when the speaker started the turn.
  `assistant` is the agent; `user` is the other side. Texts the caller sends
  during the call are not in it (they arrive as `sms.received`).
- `duration_seconds` runs from answer to hang-up. It is null when the call was never answered.
- `summary` comes from your workspace's own LLM when you set one (Hail's models
  back it up only when you turned fallback on). Otherwise it comes from Hail's
  chain, the one calls and texts use (Anthropic, then OpenAI, then Gemini). A
  per-call `llm` from `POST /v1/calls` is not used. When the model fails,
  `summary` is null and the event is still sent.
- Only phone calls with at least one spoken turn are sent. A call nobody spoke on gets no event.
- Only calls that end after your `call.summarized` subscription was created
  or last changed (`PATCH`, secret rotation) are sent, so turning it on
  never replays past calls. A call not sent within 24 hours of its end is
  dropped.
- Once it is sent, `GET /v1/events` shows a `summarized` call event. It holds
  counts only (`turns`, and `summary` true or false), not the text.
- Self-hosting: the voicebot sends this event from its text worker, which
  runs only when `HAIL_INTERNAL_SECRET` and `HAIL_API_URL` are set.

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
    "body": "hello back",
    "compliance_keyword": null
  }
}
```

`compliance_keyword` is `"STOP"`, `"START"` or `"HELP"` when the text is one of
these keywords, and `null` otherwise. The same value is in the `received` event
on `GET /v1/events` (events stored before this field existed do not have it).

- `STOP`: `STOP`, `STOPALL`, `UNSUBSCRIBE`, `CANCEL`, `END` or `QUIT`. Hail
  suppresses the sender.
- `START`: `START`, `YES` or `UNSTOP` from a sender who opted out. Hail lifts
  the suppression. From anyone else, these words are a normal reply (`null`).
- `HELP`: `HELP` or `INFO`.

Matching ignores case and surrounding spaces. When Twilio sends its own opt-out
type for a text, that type decides too ([`sms_ingest.py`](https://github.com/hail-hq/hail/blob/main/core/hailhq/core/sms_ingest.py)).
The carrier or Hail answers these keywords, depending on your setup, so a bot
should not answer them too.

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

The `detail` field varies by event type. `bounced` and `complained` carry SES metadata. `delivered` and `delivery_delayed` carry SES status details. `opened` and `clicked` carry `ip_address` and `user_agent`; `clicked` adds `link` (the event time is the sibling `occurred_at` field). Both only occur for emails sent with an HTML body: the tracking pixel and link rewriting live in the HTML part, so plain-text-only emails never produce them. For `bounced`, the provider-neutral `hard` flag distinguishes hard bounces from soft bounces. Only hard bounces move the email to `status=bounced` and count toward `bounced_hard` in `GET /emails/stats`.

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

## Test an endpoint

Send one `webhook.test` event before you save a subscription, and see your
receiver's answer:

```bash
curl https://api.hail.so/v1/webhooks/test \
  -H "Authorization: Bearer $HAIL_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"target_url":"https://receiver.example.com/hooks","authorization":"Bearer YOUR_RECEIVER_KEY"}'
```

```json
{
  "ok": true,
  "status_code": 200,
  "duration_ms": 182,
  "response_body": "ok",
  "error": null,
  "signed": false
}
```

It sends the same headers as a delivery, with `X-Hail-Event: webhook.test`.
Nothing is stored or retried, and a failed test does not count toward
auto-disable. A new endpoint has no signing secret yet, so its test carries no
`X-Hail-Signature`. Pass `subscription_id` to test a saved subscription: its
secret signs the test, and an omitted `authorization` sends the saved header to
the saved URL. Schemas: `WebhookTestRequest` and `WebhookTestResponse` in
[`openapi/openapi.yaml`](https://github.com/hail-hq/hail/blob/main/openapi/openapi.yaml).

## Chat SDK

The published [Hail adapter](https://www.npmjs.com/package/@hail-hq/chat-sdk-adapter) connects SMS to a [Vercel Chat SDK](https://chat-sdk.dev) bot. Hail also supports email and phone calls through its APIs; this adapter currently exposes SMS.

Install the adapter and a Chat SDK state backend:

```bash
pnpm add @hail-hq/chat-sdk-adapter chat @chat-adapter/state-memory
```

```typescript
import { Chat } from "chat";
import { createMemoryState } from "@chat-adapter/state-memory";
import { createHailAdapter } from "@hail-hq/chat-sdk-adapter";

export const bot = new Chat({
  userName: "mybot",
  adapters: { hail: createHailAdapter() },
  state: createMemoryState(),
});

bot.onDirectMessage(async (thread, message) => {
  await thread.post(`You said: ${message.text}`);
});
```

Set `HAIL_API_KEY`, `HAIL_FROM_NUMBER` (your Hail-owned E.164 number), and
`HAIL_WEBHOOK_SECRET`. Subscribe to `sms.received` and route the webhook to
`bot.webhooks.hail(request)`. Set `HAIL_RECIPIENT_CONSENT=true` only when you
attest to the required consent for all recipients; otherwise sending fails.
Clear the number's `sms_agent_id` if a Hail text agent would also answer it.
Memory state needs no external database. Use a shared state adapter, such as Redis or PostgreSQL, for multiple instances or serverless deployments; Redis is not a Hail requirement.

The adapter verifies signatures, isolates conversations by phone pair, and
supports SMS replies. It requires Node.js 20+ and `chat >=4.44.0`.
Refer to the [adapter setup, configuration, and limitations](https://github.com/hail-hq/chat-sdk-adapter)
and its [Next.js example](https://github.com/hail-hq/chat-sdk-adapter/tree/main/examples/nextjs).

See the [0.1.0 release announcement](https://github.com/hail-hq/chat-sdk-adapter/releases/tag/v0.1.0) for the published package and supported capabilities. Hail maintains the adapter; a Vercel vendor-official listing requires separate acceptance.
