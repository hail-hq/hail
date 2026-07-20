# Webhook events for calls (+ missing SMS/email) — design

**Date:** 2026-07-20
**Status:** Approved design, pre-implementation
**Spec A of two.** Spec B (the `n8n-nodes-hail` community node) is built _after_ this
ships and lives in the new `hail-hq/n8n-nodes` repo. This spec is a prerequisite: the
node's Trigger reads its event list from Hail's subscribable enum, so every event added
here becomes triggerable in n8n with no node change.

## Problem

Hail persists full lifecycle status for calls (`CallStatus`) and SMS (`SmsStatus`), but the
webhook subscription surface only exposes email events plus `sms.received`. Consumers —
including the planned n8n Trigger node — cannot subscribe to call outcomes or SMS delivery
results at all.

Current subscribable set (`core/hailhq/core/schemas.py`, `WEBHOOK_EVENT_TYPES`):
`email.received`, `email.delivered`, `email.delivery_delayed`, `email.bounced`,
`email.complained`, `email.opened`, `email.clicked`, `email.received.suppressed`,
`sms.received`.

## Goal

Make call lifecycle and the missing SMS/email outcomes subscribable, using the existing
fan-out + signed-delivery machinery. Additive and backward-compatible — no existing
subscription or payload changes.

## Non-goals

- No new delivery transport, retry, or signing scheme — reuse `webhook_fanout` /
  `webhook_worker` / `X-Hail-Signature` as-is.
- No inbound-call events (`call.received`) — inbound voice is not GA; add when it lands.
- No n8n node work — that is Spec B.
- No new provider _SDKs_ or channels. One new **callback route** (Twilio SMS status) is in
  scope because it is the only way `sms.delivered`/`sms.undelivered` can ever fire; calls
  need no new capture (already ingested via LiveKit).

## Events to add

Final set — **9 events** (corrected after code investigation; see Reachability below):

| Channel | New events                                                                      |
| ------- | ------------------------------------------------------------------------------- |
| Call    | `call.answered`, `call.completed`, `call.failed`, `call.busy`, `call.no_answer` |
| SMS     | `sms.delivered`, `sms.undelivered`, `sms.failed`                                |
| Email   | `email.send_failed`                                                             |

`call.answered` maps to the `in_progress` status transition (the human-facing name is
"answered"). `call.queued` / `call.dialing` and `sms.queued` / `sms.sent` are omitted as
low-signal.

### Reachability (why 9, not 11)

Code investigation corrected two assumptions:

- **Calls are placed over LiveKit SIP, not Twilio REST.** There is no Twilio call, hence no
  Twilio call-status callback to ingest. Call outcomes are **already captured** from LiveKit
  `DisconnectReason` in the voicebot (`agent.py` `_DISCONNECT_REASON_MAP` → `busy` on
  `USER_REJECTED`, `no_answer` on `USER_UNAVAILABLE`, `failed` on trunk/timeout/media), plus
  `in_progress`/`completed` on the hot path and `failed` via `calls.py` / `reconcile.py`. So
  all five call events need **only fan-out wiring** at existing status-write sites — no
  ingestion.
- **`call.ringing` and `call.canceled` are dropped — no data source exists.** LiveKit does
  not expose an outbound `ringing` signal (`agent.py:432-434`), and nothing anywhere writes
  `canceled`. Including them would define events that can never fire.
- **`sms.delivered` / `sms.undelivered` require net-new ingestion.** Nothing writes those
  statuses today; `models.py:611` calls the Twilio status-callback ingest "planned." Building
  it is the one genuinely new subsystem in this spec.

## Design

### 1. Enum + contract

- Add the nine events to the `WebhookEventType` Literal in `core/hailhq/core/schemas.py`
  (schemas.py:848). The `WebhookSubscriptionCreate.event_types` /
  `WebhookSubscriptionPatch.event_types` fields already type against `WebhookEventType`, so
  they pick up the new values automatically.
- Regenerate `openapi/openapi.yaml` in the same PR (repo invariant: OpenAPI is source of
  truth for the CLI).

### 2. Fan-out

- Add `fanout_call_event(db, *, organization_id, event_type, event_id, data)` to
  `core/hailhq/core/webhook_fanout.py`, mirroring the existing `fanout_sms_event` thin
  wrapper (webhook_fanout.py:104 — delegates to `fanout_email_event` with
  `email_domain_id=None`). Export it in `__all__`.
- `sms.delivered` / `sms.undelivered` / `sms.failed` reuse the existing `fanout_sms_event`;
  only the `event_type` string differs.
- `email.send_failed` reuses `fanout_email_event`.
- Note: `build_event_data` (webhook_fanout.py:24) is email-shaped only; call and SMS events
  pass a small inline `data` dict (as `sms.received` already does at sms_ingest.py:226).

### 3. Emit points (one fan-out call per real status transition)

All call statuses are already written; wiring means adding a `fanout_call_event` alongside
each existing write, gated on the write actually landing (use the guarded `UPDATE ...
RETURNING` so a no-op update does not emit):

- **Calls**
  - `call.answered` (`status="in_progress"`): `voicebot/hailhq/voicebot/agent.py:428`.
  - `call.completed` + terminal `call.failed`/`call.busy`/`call.no_answer`: the `on_call_end`
    UPDATE at `voicebot/hailhq/voicebot/agent.py:539` (status = `status_override or
"completed"`). Map the persisted status to the matching `call.<status>` event.
  - `call.failed` (setup failure): `api/hailhq/api/routes/calls.py:403`.
  - `call.failed` (stale-call backstop): `core/hailhq/core/reconcile.py:81`.
- **SMS**
  - `sms.failed` (transport / carrier reject): existing writes at
    `api/hailhq/api/routes/sms.py:99` and `:120`.
  - `sms.delivered` / `sms.undelivered`: emitted from the **new** status-callback ingest
    (§6), after the status + `SmsEvent` are persisted.
- **Email**: `email.send_failed` at `api/hailhq/api/routes/emails.py:363` (the outbound
  `status="failed"` path).

Prefer one choke point per transition so each fires exactly one event.

### 4. Payload

Reuse `build_event_data` / `build_event_payload`. `data` is the resource snapshot
(`CallResponse`-shaped for calls, `SmsResponse`-shaped for SMS) plus the standard event
envelope (`event_type`, `created_at`, ids). No new payload schema.

### 5. Idempotency

Emit once per transition. The delivery insert already uses `ON CONFLICT DO NOTHING`; ensure
retried/replayed status updates (e.g. duplicate carrier callbacks) do not double-fire —
gate the emit on an actual status _change_, not on every write.

### 6. SMS delivery-status ingestion (new subsystem — prerequisite for `sms.delivered`/`sms.undelivered`)

Nothing ingests Twilio message-status callbacks today. Add:

- **Send side:** pass `status_callback=<url>` in `messages.create`
  (`core/hailhq/core/providers/sms/twilio.py:46`). Build the URL with the
  `hailhq.core.urls` helpers against `settings.hail_api_url` (never f-string — repo
  invariant) pointing at the new route.
- **Route:** `POST /sms/status` (mirror the existing inbound handler
  `api/hailhq/api/routes/sms.py:314`): `include_in_schema=False`, parse the form body,
  verify `X-Twilio-Signature` via `verify_twilio_signature(url, params, signature,
settings.twilio_auth_token)` → `403` on failure (same pattern as sms.py:325). URL is
  reconstructed from `settings.hail_api_url`, not `request.url`.
- **Status map:** Twilio `MessageStatus` → `SmsStatus`: `delivered`→`delivered`,
  `undelivered`→`undelivered`, `failed`→`failed`, `sent`→`sent`. Ignore intermediate
  `queued`/`sending`/`accepted` (no state change worth an event).
- **Persist:** look up the `Sms` row by `provider_message_sid` (= Twilio `MessageSid`),
  update `status` only on a real transition, and insert an `SmsEvent` (`kind="state_change"`)
  using `pg_insert(...).on_conflict_do_nothing(constraint="sms_events_dedup_uq")` — the
  constraint is already sized for at-least-once redelivery (models.py:636). Skip fan-out when
  nothing was inserted (mirror `email_delivery_events.py:120-161`).
- **Fan-out:** on a real insert, `fanout_sms_event(..., event_type="sms.<status>")`.

## Risks / open questions

1. **`in_progress` naming.** Wire event is `call.answered` while the DB status is
   `in_progress`. Keep the status→event mapping in one place (a small dict) to avoid drift.
2. **Voicebot emits fan-out.** Call events fire from the voicebot service (`agent.py`), which
   already holds an `AsyncSession` and the call's `organization_id`. Confirm the fan-out call
   sits inside the same transaction as the status UPDATE so a rolled-back status change can't
   leave an orphan delivery.
3. **Twilio status-callback auth.** The callback is public; `X-Twilio-Signature` is the only
   credential. Fail closed (`403`) on missing/invalid signature — `verify_twilio_signature`
   already returns `False` when the signature or auth token is empty.

## Testing

- Unit: `fanout_call_event` enqueues one delivery per matching subscription; the
  `WebhookEventType` Literal accepts the nine new values; non-subscribed events enqueue
  nothing.
- Unit: Twilio `MessageStatus` → `SmsStatus` map (delivered/undelivered/failed/sent; ignores
  intermediate).
- SMS callback route: valid `X-Twilio-Signature` → status persisted + `sms.<status>`
  fanned out; bad/missing signature → `403`; duplicate callback (same MessageSid+status) →
  no second `SmsEvent`, no second delivery (dedup).
- Idempotency: a repeated status write does not enqueue a second delivery.
- Integration: subscribe to `call.*`, drive a call answered→completed, assert delivery rows +
  valid `X-Hail-Signature`.

## Rollout

Purely additive. Existing subscriptions keep their event sets; new events only reach
subscribers who opt in. No migration beyond the (additive) enum/OpenAPI change.
