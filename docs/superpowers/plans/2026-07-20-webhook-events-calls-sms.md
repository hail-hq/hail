# Webhook Events for Calls + SMS/Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make call lifecycle + missing SMS/email outcomes subscribable webhook events, and build the one prerequisite that's missing — Twilio SMS delivery-status ingestion.

**Architecture:** Nine new `WebhookEventType` values fan out through the existing signed-delivery machinery. Call/SMS/email failure + call outcome events are wired at status-write sites that already exist (calls ride LiveKit, not Twilio). `sms.delivered`/`sms.undelivered` require a net-new Twilio message-status callback route, which persists the status via the pre-sized `sms_events_dedup_uq` dedup then fans out.

**Tech Stack:** Python 3.12, FastAPI, SQLAlchemy async, Pydantic v2, Twilio SDK, pytest. Package layout: `core/` (shared lib), `api/` (FastAPI), `voicebot/` (LiveKit worker).

## Global Constraints

- **OpenAPI is source of truth for the CLI** — regenerate `openapi/openapi.yaml` in the same PR as any route/schema change (repo invariant).
- **URLs are not strings** — build callback URLs with `hailhq.core.urls` helpers (`join_url`) against `settings.hail_api_url`; never f-string, never `request.url`.
- **No new env vars** — reuse `settings.hail_api_url` and `settings.twilio_auth_token`. If that turns out false, update `.env.example` in the same commit under the Twilio section.
- **Shared models/schemas live in `core/`** — no duplicated Call/Sms/Email schemas.
- **Style:** ruff (`--fix`) + black; mypy + pytest green before commit. Conventional Commits. **No `Co-Authored-By` / AI-attribution trailer.**
- **Nine events, verbatim:** `call.answered`, `call.completed`, `call.failed`, `call.busy`, `call.no_answer`, `sms.delivered`, `sms.undelivered`, `sms.failed`, `email.send_failed`. `call.ringing`/`call.canceled` are intentionally excluded (no data source).
- **Emit-once rule:** every fan-out call is gated on the status write actually landing (guarded `UPDATE ... rowcount > 0`, or the dedup insert returning a row). A no-op / redelivered update must not emit.
- **Branch:** `feat/webhook-events-calls-sms`. Do not merge to `main` locally; integration is via PR.

---

## File Structure

| File                                           | Change                                                 | Responsibility                                |
| ---------------------------------------------- | ------------------------------------------------------ | --------------------------------------------- |
| `core/hailhq/core/schemas.py`                  | modify (`WebhookEventType` @848)                       | add 9 event literals                          |
| `core/hailhq/core/webhook_fanout.py`           | modify                                                 | add `fanout_call_event`                       |
| `core/hailhq/core/providers/sms/status_map.py` | create                                                 | Twilio `MessageStatus` → `SmsStatus` map      |
| `core/hailhq/core/providers/sms/base.py`       | modify                                                 | add `status_callback_url` param to `send_sms` |
| `core/hailhq/core/providers/sms/twilio.py`     | modify                                                 | pass `status_callback` to `messages.create`   |
| `voicebot/hailhq/voicebot/agent.py`            | modify (@428, @535-561)                                | fan out `call.answered` + terminal `call.*`   |
| `api/hailhq/api/routes/calls.py`               | modify (@403)                                          | fan out `call.failed` on setup failure        |
| `core/hailhq/core/reconcile.py`                | modify (@81)                                           | fan out `call.failed` on sweeper close        |
| `api/hailhq/api/routes/emails.py`              | modify (@363)                                          | fan out `email.send_failed`                   |
| `api/hailhq/api/routes/sms.py`                 | modify (`deliver_sms` @85-152; new `POST /sms/status`) | fan out `sms.failed`; ingest delivery status  |
| `openapi/openapi.yaml`                         | regenerate                                             | contract                                      |
| `docs/setup/webhooks.md`                       | modify                                                 | document the 9 events                         |

---

## Task 1: Add the nine event types to `WebhookEventType`

**Files:**

- Modify: `core/hailhq/core/schemas.py:848-858`
- Test: `core/tests/test_webhook_schemas.py` (create if absent; else add to the nearest existing schema test)
- Regenerate: `openapi/openapi.yaml`

**Interfaces:**

- Produces: the `WebhookEventType` Literal now accepts the 9 new strings. `WebhookSubscriptionCreate.event_types` / `WebhookSubscriptionPatch.event_types` inherit them automatically (they already type against `WebhookEventType`).

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_webhook_schemas.py
import pytest
from pydantic import ValidationError
from hailhq.core.schemas import WebhookSubscriptionCreate

NEW_EVENTS = [
    "call.answered", "call.completed", "call.failed", "call.busy", "call.no_answer",
    "sms.delivered", "sms.undelivered", "sms.failed", "email.send_failed",
]

@pytest.mark.parametrize("event", NEW_EVENTS)
def test_subscription_accepts_new_event(event):
    sub = WebhookSubscriptionCreate(target_url="https://x.test/hook", event_types=[event])
    assert sub.event_types == [event]

def test_subscription_rejects_unknown_event():
    with pytest.raises(ValidationError):
        WebhookSubscriptionCreate(target_url="https://x.test/hook", event_types=["call.ringing"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_webhook_schemas.py -v`
Expected: FAIL — `ValidationError` for each new event (not yet in the Literal).

- [ ] **Step 3: Add the literals**

In `core/hailhq/core/schemas.py`, extend the `WebhookEventType` Literal (starts at line 848):

```python
WebhookEventType = Literal[
    "email.received",
    "email.delivered",
    "email.delivery_delayed",
    "email.bounced",
    "email.complained",
    "email.opened",
    "email.clicked",
    "email.received.suppressed",
    "email.send_failed",
    "sms.received",
    "sms.delivered",
    "sms.undelivered",
    "sms.failed",
    "call.answered",
    "call.completed",
    "call.failed",
    "call.busy",
    "call.no_answer",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_webhook_schemas.py -v`
Expected: PASS (all parametrized cases + the rejection case).

- [ ] **Step 5: Regenerate OpenAPI**

Run the repo's spec generator (see `scripts/` — e.g. `cd api && uv run python -m hailhq.api.export_openapi > ../openapi/openapi.yaml`, or the documented command in `docs/operations.md`). Confirm the 9 events appear in `WebhookSubscriptionCreate.event_types.enum`:

Run: `grep -c "call.answered\|sms.delivered\|email.send_failed" openapi/openapi.yaml`
Expected: ≥ 3.

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/schemas.py core/tests/test_webhook_schemas.py openapi/openapi.yaml
git commit -m "feat(webhooks): add call/sms/email lifecycle event types"
```

---

## Task 2: Add `fanout_call_event`

**Files:**

- Modify: `core/hailhq/core/webhook_fanout.py`
- Test: `core/tests/test_webhook_fanout.py` (create if absent)

**Interfaces:**

- Consumes: `fanout_email_event` (webhook_fanout.py:61).
- Produces: `async def fanout_call_event(db: AsyncSession, *, organization_id: UUID, event_type: str, event_id: UUID, data: dict[str, Any]) -> int` — returns number of delivery rows inserted. Call-shaped wrapper (no `email_domain_id`).

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_webhook_fanout.py
import uuid
import pytest
from hailhq.core.webhook_fanout import fanout_call_event
from hailhq.core.models import WebhookSubscription, WebhookDelivery
from sqlalchemy import select

@pytest.mark.asyncio
async def test_fanout_call_event_inserts_for_matching_sub(db_session, make_org):
    org_id = await make_org(db_session)
    db_session.add(WebhookSubscription(
        organization_id=org_id, target_url="https://x.test/h",
        status="active", event_types=["call.completed"], secret="whsec_x",
    ))
    await db_session.flush()
    n = await fanout_call_event(
        db_session, organization_id=org_id, event_type="call.completed",
        event_id=uuid.uuid4(), data={"id": "c1", "status": "completed"},
    )
    assert n == 1
    rows = (await db_session.execute(select(WebhookDelivery))).scalars().all()
    assert rows[0].event_type == "call.completed"
    assert rows[0].email_domain_id is None

@pytest.mark.asyncio
async def test_fanout_call_event_skips_non_matching(db_session, make_org):
    org_id = await make_org(db_session)
    db_session.add(WebhookSubscription(
        organization_id=org_id, target_url="https://x.test/h",
        status="active", event_types=["call.failed"], secret="whsec_x",
    ))
    await db_session.flush()
    n = await fanout_call_event(
        db_session, organization_id=org_id, event_type="call.completed",
        event_id=uuid.uuid4(), data={"id": "c1"},
    )
    assert n == 0
```

> If `db_session` / `make_org` fixtures don't exist under this name, reuse the async DB fixtures already used by `core/tests/` (grep the existing webhook/email-event tests for the fixture names and match them). Do not invent a new DB harness.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_webhook_fanout.py -v`
Expected: FAIL — `ImportError: cannot import name 'fanout_call_event'`.

- [ ] **Step 3: Implement**

In `core/hailhq/core/webhook_fanout.py`, add after `fanout_sms_event` (ends line 123) and extend `__all__`:

```python
__all__ = ["build_event_data", "fanout_email_event", "fanout_sms_event", "fanout_call_event"]


async def fanout_call_event(
    db: AsyncSession,
    *,
    organization_id: UUID,
    event_type: str,
    event_id: UUID,
    data: dict[str, Any],
) -> int:
    """Insert one WebhookDelivery per active subscription whose event_types
    includes event_type. Call-shaped wrapper — calls have no domain, so
    email_domain_id is always None."""
    return await fanout_email_event(
        db,
        organization_id=organization_id,
        email_domain_id=None,
        event_type=event_type,
        event_id=event_id,
        data=data,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_webhook_fanout.py -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/webhook_fanout.py core/tests/test_webhook_fanout.py
git commit -m "feat(webhooks): add fanout_call_event"
```

---

## Task 3: Fan out `call.answered` + terminal `call.*` from the voicebot

**Files:**

- Modify: `voicebot/hailhq/voicebot/agent.py` (answered write @428; `on_call_end` UPDATE @535-561)
- Test: `voicebot/tests/test_agent_call_events.py` (create; match existing voicebot test fixtures)

**Interfaces:**

- Consumes: `fanout_call_event` (Task 2).
- Produces: a module-level `_STATUS_TO_CALL_EVENT` mapping used by the emit sites.

**Context:** `on_call_end` (agent.py:535) already does a guarded `UPDATE Call ... WHERE status NOT IN TERMINAL` and sets `transitioned = rowcount > 0`, then writes a `CallEvent` inside `if transitioned:`. The answered write at agent.py:428 sets `status="in_progress"`. Both are the correct choke points. The `on_call_end` SELECT that produces `row` (unpacked at agent.py:505) must include `organization_id` so fan-out has it — verify and extend the SELECT if absent.

- [ ] **Step 1: Write the failing test**

```python
# voicebot/tests/test_agent_call_events.py
import pytest
from hailhq.voicebot.agent import _STATUS_TO_CALL_EVENT

def test_status_to_call_event_covers_reachable_terminals():
    assert _STATUS_TO_CALL_EVENT["in_progress"] == "call.answered"
    assert _STATUS_TO_CALL_EVENT["completed"] == "call.completed"
    assert _STATUS_TO_CALL_EVENT["failed"] == "call.failed"
    assert _STATUS_TO_CALL_EVENT["busy"] == "call.busy"
    assert _STATUS_TO_CALL_EVENT["no_answer"] == "call.no_answer"

def test_status_to_call_event_excludes_unreachable():
    # ringing/canceled have no data source and must not be emittable
    assert "ringing" not in _STATUS_TO_CALL_EVENT
    assert "canceled" not in _STATUS_TO_CALL_EVENT
```

> Add a behavioral test too if the voicebot suite has an `on_call_end` harness: drive a call to `completed` and assert one `call.completed` delivery row is written, and that a second (already-terminal) `on_call_end` writes none. Reuse whatever session/Call fixtures the existing `voicebot/tests` use.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd voicebot && uv run pytest tests/test_agent_call_events.py -v`
Expected: FAIL — `ImportError: cannot import name '_STATUS_TO_CALL_EVENT'`.

- [ ] **Step 3: Add the mapping + emit at both sites**

Near the top of `voicebot/hailhq/voicebot/agent.py` (beside `_DISCONNECT_REASON_MAP`):

```python
from hailhq.core.webhook_fanout import fanout_call_event

# Only statuses that have a real data source are emittable. `ringing` and
# `canceled` are deliberately absent — the voicebot never produces them.
_STATUS_TO_CALL_EVENT: dict[str, str] = {
    "in_progress": "call.answered",
    "completed": "call.completed",
    "failed": "call.failed",
    "busy": "call.busy",
    "no_answer": "call.no_answer",
}
```

At the answered write (agent.py:428), after the `UPDATE ... status="in_progress"` executes, gate on its rowcount and emit (same transaction):

```python
result = await session.execute(
    update(Call)
    .where(Call.id == call_id, Call.status == "dialing")
    .values(status="in_progress", answered_at=now)
)
if (result.rowcount or 0) > 0:
    await fanout_call_event(
        session,
        organization_id=organization_id,
        event_type=_STATUS_TO_CALL_EVENT["in_progress"],
        event_id=call_id,
        data={"id": str(call_id), "status": "in_progress"},
    )
```

> `organization_id` must be in scope here. If the answered path doesn't already load it, add `Call.organization_id` to the row fetch that guards this write (grep for how `call_id` is obtained in `mark_call_answered`).

In `on_call_end`, inside the existing `if transitioned:` block (after the `CallEvent` add, agent.py:561), emit the terminal event — only when the status maps:

```python
event_type = _STATUS_TO_CALL_EVENT.get(final_status)
if event_type is not None:
    await fanout_call_event(
        session,
        organization_id=organization_id,
        event_type=event_type,
        event_id=call_id,
        data={"id": str(call_id), "status": final_status, "end_reason": final_end_reason},
    )
```

> Ensure `organization_id` is unpacked from the `on_call_end` SELECT (the tuple at agent.py:505). If missing, add `Call.organization_id` to that SELECT's column list and to the unpack.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd voicebot && uv run pytest tests/test_agent_call_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add voicebot/hailhq/voicebot/agent.py voicebot/tests/test_agent_call_events.py
git commit -m "feat(webhooks): emit call.answered and terminal call events from voicebot"
```

---

## Task 4: Fan out `call.failed` at the API + reconciler sites

**Files:**

- Modify: `api/hailhq/api/routes/calls.py:399-407` (setup-failure UPDATE)
- Modify: `core/hailhq/core/reconcile.py:75-90` (sweeper UPDATE)
- Test: add to `api/tests/test_calls.py` and `core/tests/test_reconcile.py` (match existing names)

**Interfaces:**

- Consumes: `fanout_call_event` (Task 2).

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_calls.py (add)
@pytest.mark.asyncio
async def test_setup_failure_emits_call_failed(client, db_session, subscribe_to):
    await subscribe_to(db_session, ["call.failed"])
    # Force the LiveKit dispatch to raise so create_call hits the failure UPDATE.
    # (reuse the existing monkeypatch/mocking pattern this test module already uses
    #  to make LiveKit calls fail; assert a WebhookDelivery row with event_type
    #  'call.failed' exists after the call attempt.)
    ...
```

```python
# core/tests/test_reconcile.py (add)
@pytest.mark.asyncio
async def test_sweeper_emits_call_failed(db_session, make_stale_call, subscribe_to):
    await subscribe_to(db_session, ["call.failed"])
    call = await make_stale_call(db_session)  # non-terminal, past the stale threshold
    from hailhq.core.reconcile import sweep_stale_calls
    await sweep_stale_calls(db_session)
    rows = (await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_type == "call.failed"))
    ).scalars().all()
    assert len(rows) == 1
```

> `subscribe_to` / `make_stale_call` are helpers to add — or reuse existing fixtures if the modules already have them. Keep the assertion (one `call.failed` delivery) even if the setup boilerplate differs.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_calls.py -k call_failed -v` and `cd core && uv run pytest tests/test_reconcile.py -k call_failed -v`
Expected: FAIL — no delivery row emitted yet.

- [ ] **Step 3: Emit at both sites**

`api/hailhq/api/routes/calls.py` — the setup-failure UPDATE (399-407) is guarded on `status NOT IN terminal`. Capture its result and emit on transition:

```python
result = await db.execute(
    update(Call)
    .where(Call.id == call.id, Call.status.not_in(TERMINAL_CALL_STATUSES))
    .values(status="failed", end_reason=..., ended_at=...)  # keep existing values
)
if (result.rowcount or 0) > 0:
    await fanout_call_event(
        db,
        organization_id=call.organization_id,
        event_type="call.failed",
        event_id=call.id,
        data={"id": str(call.id), "status": "failed"},
    )
```

`core/hailhq/core/reconcile.py` — the sweeper UPDATE at 75-90 sets `status="failed"`. It already returns which rows it force-closed (it writes a `CallEvent` per closed call). For each closed call, emit `call.failed` with its `organization_id`. Add `Call.organization_id` (and `Call.id`) to the sweeper's `RETURNING`/selection if not already fetched, then:

```python
for closed in closed_calls:  # rows the sweeper transitioned this tick
    await fanout_call_event(
        db,
        organization_id=closed.organization_id,
        event_type="call.failed",
        event_id=closed.id,
        data={"id": str(closed.id), "status": "failed", "end_reason": "sweeper_timeout"},
    )
```

Import `fanout_call_event` in both files.

- [ ] **Step 4: Run tests to verify they pass**

Run the two commands from Step 2.
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/calls.py core/hailhq/core/reconcile.py api/tests/test_calls.py core/tests/test_reconcile.py
git commit -m "feat(webhooks): emit call.failed on setup failure and sweeper close"
```

---

## Task 5: Fan out `email.send_failed`

**Files:**

- Modify: `api/hailhq/api/routes/emails.py:355-370` (outbound `status="failed"` path)
- Test: add to `api/tests/test_emails.py`

**Interfaces:**

- Consumes: `fanout_email_event` (webhook_fanout.py:61).

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_emails.py (add)
@pytest.mark.asyncio
async def test_send_failure_emits_email_send_failed(db_session, subscribe_to, force_email_send_error):
    await subscribe_to(db_session, ["email.send_failed"])
    # Trigger the outbound failure branch (reuse the module's existing way of
    # making the email provider raise / return failure).
    ...
    rows = (await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_type == "email.send_failed"))
    ).scalars().all()
    assert len(rows) == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_emails.py -k send_failed -v`
Expected: FAIL.

- [ ] **Step 3: Emit at the failure site**

In `api/hailhq/api/routes/emails.py`, at the outbound `status="failed"` write (~363), after the row is updated and inside the same transaction:

```python
await fanout_email_event(
    db,
    organization_id=email.organization_id,
    email_domain_id=email.email_domain_id,
    event_type="email.send_failed",
    event_id=email.id,
    data=build_event_data(
        email_id=str(email.id),
        direction="outbound",
        from_address=email.from_address,
        to_addresses=email.to_addresses,
        subject=email.subject,
        message_id=email.message_id,
        in_reply_to=None,
        spam_verdict=None, virus_verdict=None, spf_verdict=None,
        dkim_verdict=None, dmarc_verdict=None, raw_url=None, attachments=[],
    ),
)
```

> Match the exact field names on the `Email` row (grep the model). Use `build_event_data` so the outbound payload matches the email delivery-event shape.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_emails.py -k send_failed -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/emails.py api/tests/test_emails.py
git commit -m "feat(webhooks): emit email.send_failed on outbound failure"
```

---

## Task 6: Fan out `sms.failed` from `deliver_sms`

**Files:**

- Modify: `api/hailhq/api/routes/sms.py:85-152` (`deliver_sms` — transport-failure branch @99-108 and carrier-reject branch @119-140)
- Test: add to `api/tests/test_sms.py`

**Interfaces:**

- Consumes: `fanout_sms_event` (webhook_fanout.py:104).

**Context:** both failure branches set `sms.status="failed"`, add an `SmsEvent`, then `await db.commit()`. Emit `sms.failed` **before** each commit so it rides the same transaction.

- [ ] **Step 1: Write the failing test**

```python
# api/tests/test_sms.py (add)
@pytest.mark.asyncio
async def test_deliver_sms_transport_failure_emits_sms_failed(db_session, subscribe_to, failing_provider, make_queued_sms):
    await subscribe_to(db_session, ["sms.failed"])
    sms = await make_queued_sms(db_session)
    from hailhq.api.routes.sms import deliver_sms
    await deliver_sms(db_session, failing_provider, sms)  # provider.send_sms raises
    rows = (await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_type == "sms.failed"))
    ).scalars().all()
    assert len(rows) == 1
```

> Add a second case for carrier-rejection (provider returns `status="undelivered"` / an `error_code`) asserting exactly one `sms.failed` delivery.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_sms.py -k sms_failed -v`
Expected: FAIL.

- [ ] **Step 3: Emit in both branches**

In `deliver_sms`, transport-failure branch (before `await db.commit()` at line 108):

```python
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type="sms.failed",
            event_id=sms.id,
            data={"id": str(sms.id), "to": sms.to_e164, "from": sms.from_e164, "status": "failed", "reason": "provider_error"},
        )
        await db.commit()
        return "provider_error"
```

Carrier-reject branch — only when `carrier_rejected` is true (before `await db.commit()` at line 140):

```python
    if carrier_rejected:
        await fanout_sms_event(
            db,
            organization_id=sms.organization_id,
            event_type="sms.failed",
            event_id=sms.id,
            data={"id": str(sms.id), "to": sms.to_e164, "from": sms.from_e164, "status": "failed", "error_code": result.error_code},
        )
    await db.commit()
```

Import `fanout_sms_event` at the top of `sms.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd api && uv run pytest tests/test_sms.py -k sms_failed -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/sms.py api/tests/test_sms.py
git commit -m "feat(webhooks): emit sms.failed on transport failure and carrier rejection"
```

---

## Task 7: Twilio `MessageStatus` → `SmsStatus` map

**Files:**

- Create: `core/hailhq/core/providers/sms/status_map.py`
- Test: `core/tests/test_sms_status_map.py`

**Interfaces:**

- Produces: `def map_twilio_message_status(raw: str) -> str | None` — returns a `SmsStatus` value for a status worth persisting, or `None` for intermediate/unknown statuses.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_sms_status_map.py
import pytest
from hailhq.core.providers.sms.status_map import map_twilio_message_status

@pytest.mark.parametrize("raw,expected", [
    ("delivered", "delivered"),
    ("undelivered", "undelivered"),
    ("failed", "failed"),
    ("sent", "sent"),
    ("Delivered", "delivered"),   # case-insensitive
])
def test_maps_terminal_statuses(raw, expected):
    assert map_twilio_message_status(raw) == expected

@pytest.mark.parametrize("raw", ["queued", "sending", "accepted", "scheduled", "weird"])
def test_ignores_intermediate_and_unknown(raw):
    assert map_twilio_message_status(raw) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_sms_status_map.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# core/hailhq/core/providers/sms/status_map.py
"""Twilio message-status-callback ``MessageStatus`` → Hail ``SmsStatus``.

Only statuses that represent a persistable transition map to a value; Twilio's
intermediate lifecycle (queued/sending/accepted/scheduled) returns None so the
callback handler skips them without writing or fanning out.
"""

from __future__ import annotations

_MAP: dict[str, str] = {
    "delivered": "delivered",
    "undelivered": "undelivered",
    "failed": "failed",
    "sent": "sent",
}


def map_twilio_message_status(raw: str) -> str | None:
    return _MAP.get(raw.strip().lower())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd core && uv run pytest tests/test_sms_status_map.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/sms/status_map.py core/tests/test_sms_status_map.py
git commit -m "feat(sms): add Twilio message-status to SmsStatus map"
```

---

## Task 8: Pass `status_callback` when sending SMS

**Files:**

- Modify: `core/hailhq/core/providers/sms/base.py:23-40` (add param to `send_sms`)
- Modify: `core/hailhq/core/providers/sms/twilio.py:42-73`
- Modify: any other `SmsProvider` implementations + fakes (grep `def send_sms`)
- Modify: `api/hailhq/api/routes/sms.py` `deliver_sms` — build the callback URL and pass it
- Test: `core/tests/test_twilio_sms_provider.py` (or existing provider test)

**Interfaces:**

- Consumes: `settings.hail_api_url`, `hailhq.core.urls.join_url`.
- Produces: `send_sms(..., status_callback_url: str | None = None)` across the `SmsProvider` protocol and all implementations.

- [ ] **Step 1: Write the failing test**

```python
# core/tests/test_twilio_sms_provider.py (add)
@pytest.mark.asyncio
async def test_send_sms_passes_status_callback(monkeypatch):
    captured = {}
    class _Msgs:
        def create(self, **kw):  # mimic twilio client
            captured.update(kw)
            return type("M", (), {"sid": "SM1", "status": "queued", "num_segments": "1", "error_code": None})()
    provider = TwilioSmsProvider()
    monkeypatch.setattr(provider, "_client", type("C", (), {"messages": _Msgs()})())
    await provider.send_sms(
        from_e164="+15551110000", to_e164="+15552220000", body="hi",
        status_callback_url="https://api.hail.test/sms/status",
    )
    assert captured["status_callback"] == "https://api.hail.test/sms/status"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_twilio_sms_provider.py -k status_callback -v`
Expected: FAIL — `send_sms` has no `status_callback_url` kwarg.

- [ ] **Step 3: Thread the param through**

`core/hailhq/core/providers/sms/base.py` — add to the abstract signature:

```python
    async def send_sms(
        self,
        from_e164: str,
        to_e164: str,
        body: str,
        status_callback_url: str | None = None,
    ) -> ProviderSmsResult: ...
```

`core/hailhq/core/providers/sms/twilio.py` — accept it and pass through only when set:

```python
    async def send_sms(
        self, from_e164: str, to_e164: str, body: str,
        status_callback_url: str | None = None,
    ) -> ProviderSmsResult:
        create_kwargs: dict[str, str] = {"to": to_e164, "from_": from_e164, "body": body}
        if status_callback_url is not None:
            create_kwargs["status_callback"] = status_callback_url
        try:
            message = await asyncio.to_thread(self._client.messages.create, **create_kwargs)
        ...
```

Update every other `send_sms` implementation/fake found by `grep -rn "def send_sms" core api voicebot` to accept the new kwarg (ignore it in fakes).

`api/hailhq/api/routes/sms.py` `deliver_sms` — build the URL with the helper and pass it:

```python
from hailhq.core.urls import join_url
...
    callback_url = join_url(settings.hail_api_url, "sms/status")
    result = await provider.send_sms(
        from_e164=sms.from_e164, to_e164=sms.to_e164, body=sms.body,
        status_callback_url=callback_url,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd core && uv run pytest tests/test_twilio_sms_provider.py -v` and `cd api && uv run pytest tests/test_sms.py -v`
Expected: PASS (existing SMS tests still green — the kwarg is optional).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/sms/base.py core/hailhq/core/providers/sms/twilio.py api/hailhq/api/routes/sms.py core/tests/test_twilio_sms_provider.py
git commit -m "feat(sms): request Twilio delivery status callbacks on send"
```

---

## Task 9: `POST /sms/status` — ingest delivery status + fan out

**Files:**

- Modify: `api/hailhq/api/routes/sms.py` (add the route near the inbound handler @314)
- Test: `api/tests/test_sms_status_callback.py`

**Interfaces:**

- Consumes: `verify_twilio_signature` (core/twilio_signature.py:23), `map_twilio_message_status` (Task 7), `fanout_sms_event` (Task 2's sibling), `SmsEvent` + `sms_events_dedup_uq` (models.py:636), `settings.hail_api_url`, `settings.twilio_auth_token`.

**Behavior:** parse the Twilio form callback → verify `X-Twilio-Signature` (403 on failure) → map `MessageStatus` (skip → 200 no-op if `None`) → look up `Sms` by `provider_message_sid == MessageSid` (200 no-op if unknown) → transition-gate the status update → insert `SmsEvent` with `on_conflict_do_nothing(constraint="sms_events_dedup_uq")` → fan out `sms.<status>` only when a row was inserted → commit → 200.

- [ ] **Step 1: Write the failing tests**

```python
# api/tests/test_sms_status_callback.py
import pytest
from sqlalchemy import select
from hailhq.core.models import Sms, SmsEvent, WebhookDelivery

async def _post_status(client, sms_sid, status, *, sign=True):
    # Reuse the signing helper the inbound-SMS test already uses (grep test_sms
    # for how it builds a valid X-Twilio-Signature); form fields:
    return await client.post("/sms/status", data={"MessageSid": sms_sid, "MessageStatus": status}, headers=...)

@pytest.mark.asyncio
async def test_delivered_callback_persists_and_fans_out(client, db_session, make_sent_sms, subscribe_to):
    sms = await make_sent_sms(db_session, provider_message_sid="SM123")
    await subscribe_to(db_session, ["sms.delivered"])
    r = await _post_status(client, "SM123", "delivered")
    assert r.status_code == 200
    refreshed = await db_session.get(Sms, sms.id)
    assert refreshed.status == "delivered"
    deliveries = (await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_type == "sms.delivered"))
    ).scalars().all()
    assert len(deliveries) == 1

@pytest.mark.asyncio
async def test_bad_signature_rejected(client, db_session, make_sent_sms):
    await make_sent_sms(db_session, provider_message_sid="SM123")
    r = await _post_status(client, "SM123", "delivered", sign=False)
    assert r.status_code == 403

@pytest.mark.asyncio
async def test_duplicate_callback_is_idempotent(client, db_session, make_sent_sms, subscribe_to):
    await make_sent_sms(db_session, provider_message_sid="SM123")
    await subscribe_to(db_session, ["sms.delivered"])
    await _post_status(client, "SM123", "delivered")
    await _post_status(client, "SM123", "delivered")
    events = (await db_session.execute(
        select(SmsEvent).where(SmsEvent.kind == "state_change"))
    ).scalars().all()
    deliveries = (await db_session.execute(
        select(WebhookDelivery).where(WebhookDelivery.event_type == "sms.delivered"))
    ).scalars().all()
    assert len([e for e in events if e.payload.get("to") == "delivered"]) == 1
    assert len(deliveries) == 1

@pytest.mark.asyncio
async def test_unknown_sid_is_noop(client, db_session):
    r = await _post_status(client, "SM_nope", "delivered")
    assert r.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd api && uv run pytest tests/test_sms_status_callback.py -v`
Expected: FAIL — route 404 / not defined.

- [ ] **Step 3: Implement the route**

In `api/hailhq/api/routes/sms.py`, mirroring the inbound handler at 314-340:

```python
from datetime import datetime, timezone
from sqlalchemy.dialects.postgresql import insert as pg_insert
from hailhq.core.providers.sms.status_map import map_twilio_message_status
from hailhq.core.twilio_signature import verify_twilio_signature
from hailhq.core.urls import join_url


@router.post("/status", include_in_schema=False)
async def receive_sms_status(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict[str, str]:
    form = await request.form()
    params = {k: str(v) for k, v in form.items()}
    signature = request.headers.get("X-Twilio-Signature")
    url = join_url(settings.hail_api_url, "sms/status")
    if not verify_twilio_signature(url, params, signature, settings.twilio_auth_token):
        raise HTTPException(status_code=403, detail="invalid signature")

    new_status = map_twilio_message_status(params.get("MessageStatus", ""))
    if new_status is None:
        return {"status": "ignored"}

    sid = params.get("MessageSid")
    sms = (
        await db.execute(select(Sms).where(Sms.provider_message_sid == sid))
    ).scalar_one_or_none()
    if sms is None:
        return {"status": "unmatched"}

    occurred_at = datetime.now(timezone.utc)
    ins = (
        pg_insert(SmsEvent)
        .values(
            sms_id=sms.id,
            organization_id=sms.organization_id,
            kind="state_change",
            occurred_at=occurred_at,
            payload={"from": sms.status, "to": new_status},
        )
        .on_conflict_do_nothing(constraint="sms_events_dedup_uq")
        .returning(SmsEvent.id)
    )
    inserted_id = (await db.execute(ins)).scalar_one_or_none()
    if inserted_id is None:
        return {"status": "duplicate"}

    sms.status = new_status
    await fanout_sms_event(
        db,
        organization_id=sms.organization_id,
        event_type=f"sms.{new_status}",
        event_id=sms.id,
        data={"id": str(sms.id), "to": sms.to_e164, "from": sms.from_e164, "status": new_status},
    )
    await db.commit()
    return {"status": "applied"}
```

> Notes: (1) `occurred_at` is part of `sms_events_dedup_uq` — for real Twilio redeliveries the timestamp differs, so dedup on `(sms_id, kind, occurred_at)` won't absorb a re-POST that arrives later. If exact-once per _status_ is required, dedup on a status-derived key instead (e.g. include `to`-status in the constraint, or check "already at this status" before writing). Confirm the intended dedup granularity against `email_delivery_events.py` and match it. (2) `sms.sent` fans out too when Twilio reports `sent`; that's intended (an emittable event). If only delivered/undelivered/failed should fan out, gate the `fanout_sms_event` on `new_status in {"delivered", "undelivered", "failed"}`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd api && uv run pytest tests/test_sms_status_callback.py -v`
Expected: PASS (all four cases).

- [ ] **Step 5: Regenerate OpenAPI** (route is `include_in_schema=False`, so the spec should be unchanged — verify no diff)

Run: regenerate as in Task 1 Step 5, then `git diff --stat openapi/openapi.yaml`
Expected: no change (internal route excluded from schema).

- [ ] **Step 6: Commit**

```bash
git add api/hailhq/api/routes/sms.py api/tests/test_sms_status_callback.py
git commit -m "feat(sms): ingest Twilio delivery-status callbacks and fan out sms.delivered/undelivered"
```

---

## Task 10: Document the new events

**Files:**

- Modify: `docs/setup/webhooks.md`

- [ ] **Step 1: Update the event table**

Add the nine events to the subscribable-events list in `docs/setup/webhooks.md`, grouped by channel, with a one-line note that `sms.delivered`/`sms.undelivered` depend on carrier delivery receipts and that call events cover `answered/completed/failed/busy/no_answer` (no `ringing`/`canceled`). Keep the page to one screen (repo tenet).

- [ ] **Step 2: Verify the doc references match the enum**

Run: `grep -o "call\.[a-z_]*\|sms\.[a-z_]*\|email\.send_failed" docs/setup/webhooks.md | sort -u`
Expected: exactly the nine new events plus the pre-existing ones — no `call.ringing`/`call.canceled`.

- [ ] **Step 3: Commit**

```bash
git add docs/setup/webhooks.md
git commit -m "docs(webhooks): document call/sms/email lifecycle events"
```

---

## Final verification

- [ ] Full suites: `cd core && uv run pytest` · `cd api && uv run pytest` · `cd voicebot && uv run pytest` — all green.
- [ ] `cd api && uv run mypy hailhq` (and core) — clean.
- [ ] `ruff check --fix` + `black` across changed dirs — clean.
- [ ] `git diff --stat main...HEAD` — confirm only the files in the File Structure table changed.
- [ ] Re-run `/code-review --fix` on the branch diff before opening the PR (this is where the 8-angle scan pays off: emit-once gating, dedup granularity in Task 9, missing `organization_id` in the voicebot SELECT).
