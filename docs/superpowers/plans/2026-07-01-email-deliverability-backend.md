# Email Deliverability Tracking — Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest SES delivery/engagement events (delivered, bounced, complained, rejected, delivery_delayed, opened, clicked) into a new `email_events` table, advance `emails.status`, fan out customer webhooks, and expose per-email timelines plus account-level stats via public API, CLI, and MCP.

**Architecture:** SES configuration set → SNS topic → existing ingest Lambda (new SNS branch) → `POST /internal/ses-events` with envelope `type: "delivery_event"` → parser + `apply_delivery_event()` service in core (dedup insert, guarded status transitions, webhook fanout) → new `GET /emails/{id}/events` and `GET /emails/stats` endpoints; email events join the `GET /events` stream.

**Tech Stack:** FastAPI (async), SQLAlchemy 2.0 async + Alembic, Pydantic v2, boto3 SESv2, Terraform, Go/Cobra CLI (client codegen'd from `openapi/openapi.yaml`), FastMCP.

**Spec:** `docs/superpowers/specs/2026-07-01-email-deliverability-tracking-design.md`

## Global Constraints

- Repo: `/Users/r/playground/hail`. All paths below are relative to it.
- **OpenAPI is source of truth for the CLI**: after any API route change, regenerate `openapi/openapi.yaml` in the same PR (Task 13).
- **New env vars must land in `.env.example` in the same commit** (Task 10).
- **Provider adapters live in `core/hailhq/core/providers/<channel>/`**; `api/` never imports boto3 directly.
- **Shared models/schemas go in `core/`** — no duplication across services.
- Conventional Commits (`feat(api): …`, `feat(core): …`, `feat(cli): …`, `feat(infra): …`).
- Python: ruff + black + mypy; run `uv run pytest` from the package dir (`api/`, `core/`, `mcp/`). Go: `gofmt`, `go test ./...` from `cli/`.
- Event kinds (exact strings, used everywhere): `sent`, `delivered`, `delivery_delayed`, `bounced`, `complained`, `rejected`, `opened`, `clicked`.
- Webhook event types added (exact): `email.delivered`, `email.delivery_delayed`, `email.opened`, `email.clicked` (plus activating existing `email.bounced`, `email.complained`). **No `email.rejected`** — rejects surface as `status=failed` only.
- Status transitions (guarded, terminal never regressed): `sent → delivered`; `sent|delivered → bounced` (Permanent bounce only); `sent|delivered|bounced → complained`; `queued|sent → failed` (on `rejected`, with `end_reason`). Soft bounces, delays, opens, clicks never change status.
- Migrations: hand-written with `op.*` helpers, docstring header with Revision ID (see `api/migrations/versions/0011_…` for the pattern). Next revision is `0019`.

---

### Task 1: Migration 0019 — `email_events` table + `delivered` status

**Files:**

- Create: `api/migrations/versions/0019_email_events.py`
- Modify: `core/hailhq/core/models.py` (add `EmailEvent` after `CallEvent`; widen `emails_status_check`)
- Test: `api/tests/test_migrations.py` (existing test exercises upgrade head — no new test file)

**Interfaces:**

- Produces: `hailhq.core.models.EmailEvent` with columns `id: UUID`, `email_id: UUID (FK emails.id CASCADE)`, `organization_id: UUID`, `kind: str`, `payload: dict`, `occurred_at: datetime`, `created_at: datetime`; unique constraint `email_events_dedup_uq (email_id, kind, occurred_at)`; `emails.status` may now be `'delivered'`.

- [ ] **Step 1: Write the migration**

```python
"""email_events table + allow 'delivered' email status.

One append-only row per SES lifecycle event (and a synthetic ``sent`` row
written at send time). The dedup unique index absorbs SNS at-least-once
redelivery. ``organization_id`` is denormalized so account-level stats
aggregate without a join. The emails CHECK constraint gains 'delivered'
(Postgres can't widen a CHECK in place — drop and re-add).

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision: str = "0019"
down_revision: Union[str, None] = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "email_events",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "email_id",
            UUID(as_uuid=True),
            sa.ForeignKey("emails.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("organization_id", UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "email_id", "kind", "occurred_at", name="email_events_dedup_uq"
        ),
    )
    op.create_index(
        "email_events_email_occurred_idx", "email_events", ["email_id", "occurred_at"]
    )
    op.create_index(
        "email_events_org_occurred_kind_idx",
        "email_events",
        ["organization_id", "occurred_at", "kind"],
    )
    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','delivered','failed','bounced',"
        "'complained','received')",
    )


def downgrade() -> None:
    op.drop_constraint("emails_status_check", "emails", type_="check")
    op.create_check_constraint(
        "emails_status_check",
        "emails",
        "status IN ('queued','sent','failed','bounced','complained','received')",
    )
    op.drop_index("email_events_org_occurred_kind_idx", table_name="email_events")
    op.drop_index("email_events_email_occurred_idx", table_name="email_events")
    op.drop_table("email_events")
```

- [ ] **Step 2: Add the model to `core/hailhq/core/models.py`** — insert directly after `class CallEvent`, and update the `emails_status_check` CheckConstraint inside `class Email.__table_args__` to include `'delivered'`:

```python
class EmailEvent(Base):
    """Append-only email lifecycle event (mirrors CallEvent).

    ``organization_id`` is denormalized from the parent email so stats
    queries aggregate without a join. The (email_id, kind, occurred_at)
    unique constraint absorbs SNS at-least-once redelivery — inserts use
    ON CONFLICT DO NOTHING and skip fanout when nothing was inserted.
    """

    __tablename__ = "email_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    email_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("emails.id", ondelete="CASCADE"), nullable=False
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(TS, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        TS, server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "email_id", "kind", "occurred_at", name="email_events_dedup_uq"
        ),
        Index("email_events_email_occurred_idx", "email_id", "occurred_at"),
        Index(
            "email_events_org_occurred_kind_idx",
            "organization_id",
            "occurred_at",
            "kind",
        ),
    )
```

And in `Email.__table_args__`, change:

```python
        CheckConstraint(
            "status IN ('queued','sent','failed','bounced','complained','received')",
            name="emails_status_check",
        ),
```

to:

```python
        CheckConstraint(
            "status IN ('queued','sent','delivered','failed','bounced',"
            "'complained','received')",
            name="emails_status_check",
        ),
```

- [ ] **Step 3: Run migration + migration test**

Run: `cd api && uv run alembic upgrade head && uv run pytest tests/test_migrations.py -v`
Expected: upgrade applies cleanly; migration tests PASS.

- [ ] **Step 4: Commit**

```bash
git add api/migrations/versions/0019_email_events.py core/hailhq/core/models.py
git commit -m "feat(core): email_events table + delivered email status"
```

---

### Task 2: Schemas — event kinds, `delivered` status, webhook types, event responses

**Files:**

- Modify: `core/hailhq/core/schemas.py`
- Test: `core/tests/test_schemas_email_events.py` (create; `core/tests/` already exists — check with `ls core/tests` and follow its conftest)

**Interfaces:**

- Produces (all in `hailhq.core.schemas`):
  - `EmailEventKind = Literal["sent","delivered","delivery_delayed","bounced","complained","rejected","opened","clicked"]`
  - `EmailStatus` gains `"delivered"`; `TERMINAL_EMAIL_STATUSES` gains `"delivered"`.
  - `WebhookEventType` gains `"email.delivered"`, `"email.delivery_delayed"`, `"email.opened"`, `"email.clicked"`.
  - `EmailEventResponse {id: UUID, email_id: UUID, kind: EmailEventKind, payload: dict[str, Any], occurred_at: datetime}` (from_attributes)
  - `EmailEventListResponse {items: list[EmailEventResponse]}`
  - `SUPPORTED_RESOURCE_TYPES` gains `"email"`.
  - `EventResponse {id: UUID, source: Literal["call","email"], call_id: UUID | None, email_id: UUID | None, kind: str, payload: dict[str, Any], occurred_at: datetime}` — the new item type for `GET /events`; `EventStreamResponse.items: list[EventResponse]` (keep `CallEventResponse` class untouched for any other callers).

- [ ] **Step 1: Write the failing test** (`core/tests/test_schemas_email_events.py`)

```python
"""Schema-level contracts for email deliverability events."""

from hailhq.core import schemas


def test_email_event_kind_values():
    assert set(schemas.EmailEventKind.__args__) == {
        "sent", "delivered", "delivery_delayed", "bounced",
        "complained", "rejected", "opened", "clicked",
    }


def test_email_status_includes_delivered():
    assert "delivered" in schemas.EmailStatus.__args__
    assert "delivered" in schemas.TERMINAL_EMAIL_STATUSES


def test_webhook_event_types_include_lifecycle():
    got = set(schemas.WebhookEventType.__args__)
    assert {
        "email.delivered", "email.delivery_delayed", "email.bounced",
        "email.complained", "email.opened", "email.clicked",
    } <= got
    assert "email.rejected" not in got


def test_events_stream_supports_email_resource():
    assert "email" in schemas.SUPPORTED_RESOURCE_TYPES
    rtype, _ = schemas.parse_resource_id(
        "email:0e6f3f52-1c7e-4a75-9c67-000000000001"
    )
    assert rtype == "email"


def test_event_response_shape():
    fields = set(schemas.EventResponse.model_fields)
    assert {"id", "source", "call_id", "email_id", "kind", "payload",
            "occurred_at"} <= fields
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_schemas_email_events.py -v`
Expected: FAIL with `AttributeError: … has no attribute 'EmailEventKind'`.

- [ ] **Step 3: Implement in `core/hailhq/core/schemas.py`**

1. `SUPPORTED_RESOURCE_TYPES: tuple[str, ...] = ("call", "email")`
2. `EmailStatus = Literal["queued", "sent", "delivered", "failed", "bounced", "complained", "received"]` and `TERMINAL_EMAIL_STATUSES = frozenset({"sent", "delivered", "failed", "bounced", "complained"})`
3. After `TERMINAL_EMAIL_STATUSES`:

```python
EmailEventKind = Literal[
    "sent",
    "delivered",
    "delivery_delayed",
    "bounced",
    "complained",
    "rejected",
    "opened",
    "clicked",
]


class EmailEventResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    email_id: UUID
    kind: EmailEventKind
    payload: dict[str, Any]
    occurred_at: datetime


class EmailEventListResponse(BaseModel):
    items: list[EmailEventResponse]
```

4. Widen `WebhookEventType` (replace the existing Literal; update the comment above it — bounce/complaint ingestion now ships):

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
]
```

5. After `CallEventResponse`, add the stream item type and retype the stream response:

```python
class EventResponse(BaseModel):
    """One event on the unified GET /events stream (call or email)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    # Defaulted to "call" so the events route keeps validating bare CallEvent
    # rows between this task and the stream-union task (Task 9), which sets
    # source explicitly per branch of the union.
    source: Literal["call", "email"] = "call"
    call_id: UUID | None = None
    email_id: UUID | None = None
    kind: str
    payload: dict[str, Any]
    occurred_at: datetime
```

and change `EventStreamResponse.items` to `list[EventResponse]`.

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_schemas_email_events.py -v && cd ../api && uv run pytest tests/test_events_api.py -v`
Expected: ALL PASS, including the existing events suite (the `source` default keeps `CallEvent` rows validating until Task 9 unions the stream).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/schemas.py core/tests/test_schemas_email_events.py
git commit -m "feat(core): email event kinds, delivered status, lifecycle webhook types"
```

---

### Task 3: SES delivery-event parser

**Files:**

- Create: `core/hailhq/core/providers/email/inbound/ses_delivery.py`
- Test: `core/tests/test_ses_delivery_parser.py`

**Interfaces:**

- Produces:
  - `DeliveryEvent` frozen dataclass: `kind: str` (an `EmailEventKind` except `"sent"`), `provider_message_id: str`, `occurred_at: datetime`, `detail: dict[str, Any]`.
  - `parse_delivery_event(event: dict) -> DeliveryEvent | None` — `None` for event types we don't track (e.g. `Send`, `Subscription`, `Rendering Failure`).

- [ ] **Step 1: Write the failing test** (`core/tests/test_ses_delivery_parser.py`) — fixtures are real SES config-set event shapes (see [SES docs: event publishing examples](https://docs.aws.amazon.com/ses/latest/dg/event-publishing-retrieving-sns-examples.html)):

```python
"""Parser tests for SES configuration-set delivery events."""

from datetime import datetime, timezone

from hailhq.core.providers.email.inbound.ses_delivery import parse_delivery_event

MAIL = {
    "messageId": "0107019-real-ses-message-id-000",
    "timestamp": "2026-07-01T12:00:00.000Z",
    "source": "noreply@acme.com",
    "destination": ["bob@example.com"],
}


def test_parse_delivery():
    ev = parse_delivery_event({
        "eventType": "Delivery",
        "mail": MAIL,
        "delivery": {
            "timestamp": "2026-07-01T12:00:03.000Z",
            "recipients": ["bob@example.com"],
            "smtpResponse": "250 2.6.0 OK",
            "processingTimeMillis": 3000,
        },
    })
    assert ev is not None
    assert ev.kind == "delivered"
    assert ev.provider_message_id == MAIL["messageId"]
    assert ev.occurred_at == datetime(2026, 7, 1, 12, 0, 3, tzinfo=timezone.utc)
    assert ev.detail["smtp_response"] == "250 2.6.0 OK"
    assert ev.detail["recipients"] == ["bob@example.com"]


def test_parse_permanent_bounce():
    ev = parse_delivery_event({
        "eventType": "Bounce",
        "mail": MAIL,
        "bounce": {
            "bounceType": "Permanent",
            "bounceSubType": "General",
            "timestamp": "2026-07-01T12:00:05.000Z",
            "bouncedRecipients": [{
                "emailAddress": "bob@example.com",
                "diagnosticCode": "smtp; 550 5.1.1 user unknown",
            }],
        },
    })
    assert ev.kind == "bounced"
    assert ev.detail["bounce_type"] == "Permanent"
    assert ev.detail["bounce_sub_type"] == "General"
    assert ev.detail["recipients"] == ["bob@example.com"]
    assert ev.detail["diagnostic_code"] == "smtp; 550 5.1.1 user unknown"


def test_parse_complaint():
    ev = parse_delivery_event({
        "eventType": "Complaint",
        "mail": MAIL,
        "complaint": {
            "timestamp": "2026-07-01T12:01:00.000Z",
            "complaintFeedbackType": "abuse",
            "complainedRecipients": [{"emailAddress": "bob@example.com"}],
        },
    })
    assert ev.kind == "complained"
    assert ev.detail["complaint_feedback_type"] == "abuse"


def test_parse_reject():
    ev = parse_delivery_event({
        "eventType": "Reject",
        "mail": MAIL,
        "reject": {"reason": "Bad content"},
    })
    assert ev.kind == "rejected"
    assert ev.detail["reason"] == "Bad content"
    # Reject carries no event timestamp — falls back to mail.timestamp.
    assert ev.occurred_at == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_delivery_delay():
    ev = parse_delivery_event({
        "eventType": "DeliveryDelay",
        "mail": MAIL,
        "deliveryDelay": {
            "timestamp": "2026-07-01T12:02:00.000Z",
            "delayType": "MailboxFull",
            "expirationTime": "2026-07-02T12:00:00.000Z",
            "delayedRecipients": [{"emailAddress": "bob@example.com"}],
        },
    })
    assert ev.kind == "delivery_delayed"
    assert ev.detail["delay_type"] == "MailboxFull"


def test_parse_open_and_click():
    op = parse_delivery_event({
        "eventType": "Open",
        "mail": MAIL,
        "open": {
            "timestamp": "2026-07-01T12:05:00.000Z",
            "ipAddress": "203.0.113.9",
            "userAgent": "Mozilla/5.0",
        },
    })
    assert op.kind == "opened"
    assert op.detail["user_agent"] == "Mozilla/5.0"

    cl = parse_delivery_event({
        "eventType": "Click",
        "mail": MAIL,
        "click": {
            "timestamp": "2026-07-01T12:06:00.000Z",
            "ipAddress": "203.0.113.9",
            "userAgent": "Mozilla/5.0",
            "link": "https://acme.com/offer",
        },
    })
    assert cl.kind == "clicked"
    assert cl.detail["link"] == "https://acme.com/offer"


def test_untracked_event_type_returns_none():
    assert parse_delivery_event({"eventType": "Send", "mail": MAIL}) is None
    assert parse_delivery_event({"eventType": "Rendering Failure", "mail": MAIL}) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/test_ses_delivery_parser.py -v`
Expected: FAIL with `ModuleNotFoundError: … ses_delivery`.

- [ ] **Step 3: Implement** (`core/hailhq/core/providers/email/inbound/ses_delivery.py`)

```python
"""Parser for SES configuration-set delivery/engagement events.

These arrive via SNS (config set event destination), relayed by the
ses-ingest-lambda inside a ``{"type": "delivery_event", "event": {...}}``
envelope. This module parses the inner raw SES event into a neutral
``DeliveryEvent``; matching to an Email row and side effects live in
``hailhq.core.email_delivery_events``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["DeliveryEvent", "parse_delivery_event"]

# SES eventType → our email_events.kind. "Send" is deliberately absent:
# the API writes a synthetic ``sent`` row at send time (Task 5).
_KIND_BY_EVENT_TYPE = {
    "Delivery": "delivered",
    "Bounce": "bounced",
    "Complaint": "complained",
    "Reject": "rejected",
    "DeliveryDelay": "delivery_delayed",
    "Open": "opened",
    "Click": "clicked",
}


@dataclass(frozen=True)
class DeliveryEvent:
    kind: str
    provider_message_id: str
    occurred_at: datetime
    detail: dict[str, Any]


def _ts(value: str | None, fallback: str) -> datetime:
    raw = value or fallback
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _recipients(entries: list[dict] | None) -> list[str]:
    return [e["emailAddress"] for e in (entries or []) if e.get("emailAddress")]


def parse_delivery_event(event: dict[str, Any]) -> DeliveryEvent | None:
    kind = _KIND_BY_EVENT_TYPE.get(event.get("eventType", ""))
    if kind is None:
        return None
    mail = event["mail"]
    mail_ts: str = mail["timestamp"]

    detail: dict[str, Any]
    ts: datetime
    if kind == "delivered":
        d = event.get("delivery") or {}
        ts = _ts(d.get("timestamp"), mail_ts)
        detail = {
            "recipients": list(d.get("recipients") or []),
            "smtp_response": d.get("smtpResponse"),
            "processing_time_ms": d.get("processingTimeMillis"),
        }
    elif kind == "bounced":
        b = event.get("bounce") or {}
        ts = _ts(b.get("timestamp"), mail_ts)
        recips = b.get("bouncedRecipients") or []
        detail = {
            "bounce_type": b.get("bounceType"),
            "bounce_sub_type": b.get("bounceSubType"),
            "recipients": _recipients(recips),
            "diagnostic_code": next(
                (r["diagnosticCode"] for r in recips if r.get("diagnosticCode")),
                None,
            ),
        }
    elif kind == "complained":
        c = event.get("complaint") or {}
        ts = _ts(c.get("timestamp"), mail_ts)
        detail = {
            "complaint_feedback_type": c.get("complaintFeedbackType"),
            "recipients": _recipients(c.get("complainedRecipients")),
        }
    elif kind == "rejected":
        detail = {"reason": (event.get("reject") or {}).get("reason")}
        ts = _ts(None, mail_ts)
    elif kind == "delivery_delayed":
        dd = event.get("deliveryDelay") or {}
        ts = _ts(dd.get("timestamp"), mail_ts)
        detail = {
            "delay_type": dd.get("delayType"),
            "expiration_time": dd.get("expirationTime"),
            "recipients": _recipients(dd.get("delayedRecipients")),
        }
    else:  # opened / clicked
        o = event.get("open" if kind == "opened" else "click") or {}
        ts = _ts(o.get("timestamp"), mail_ts)
        detail = {
            "ip_address": o.get("ipAddress"),
            "user_agent": o.get("userAgent"),
        }
        if kind == "clicked":
            detail["link"] = o.get("link")

    return DeliveryEvent(
        kind=kind,
        provider_message_id=mail["messageId"],
        occurred_at=ts,
        detail=detail,
    )
```

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest tests/test_ses_delivery_parser.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/providers/email/inbound/ses_delivery.py core/tests/test_ses_delivery_parser.py
git commit -m "feat(core): parse SES configuration-set delivery events"
```

---

### Task 4: `apply_delivery_event()` — dedup insert, status transitions, fanout

**Files:**

- Create: `core/hailhq/core/email_delivery_events.py`
- Test: `api/tests/test_email_delivery_events.py` (lives in `api/tests` to reuse the Postgres fixtures from `hailhq.core.testing.fixtures` via the existing conftest)

**Interfaces:**

- Consumes: `DeliveryEvent` (Task 3), `EmailEvent` model (Task 1), `fanout_email_event` (existing).
- Produces:
  - `ApplyResult` dataclass: `email_id: UUID | None`, `inserted: bool`, `status_changed: bool`.
  - `async apply_delivery_event(db, event: DeliveryEvent, *, fanout) -> ApplyResult` — does **not** commit; caller commits.
  - `build_delivery_event_data(email, event) -> dict` — webhook `data` payload: `{"id", "kind", "occurred_at", "from_address", "to_addresses", "subject", "detail"}`.
  - Fanout event type mapping: every kind except `rejected` and `sent` fans out as `f"email.{kind}"`.

- [ ] **Step 1: Write the failing test** (`api/tests/test_email_delivery_events.py`)

```python
"""apply_delivery_event: dedup, guarded status transitions, fanout."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.email_delivery_events import apply_delivery_event
from hailhq.core.models import Email, EmailEvent
from hailhq.core.providers.email.inbound.ses_delivery import DeliveryEvent

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _mk_email(session: AsyncSession, *, status="sent", pmid=None) -> Email:
    email = Email(
        organization_id=uuid4(),
        email_domain_id=None,
        direction="outbound",
        from_address="noreply@acme.com",
        to_addresses=["bob@example.com"],
        subject="hi",
        body_text="hello",
        status=status,
        provider="ses",
        provider_message_id=pmid or f"pmid-{uuid4()}",
    )
    # Outbound rows require email_domain_id (emails_outbound_has_domain
    # CHECK) — create a minimal verified EmailDomain to satisfy it.
    from hailhq.core.models import EmailDomain

    dom = EmailDomain(
        organization_id=email.organization_id,
        kind="custom",
        domain=f"acme-{uuid4().hex[:8]}.com",
        verification_status="verified",
        dns_records=[],
        provider="ses",
    )
    session.add(dom)
    await session.flush()
    email.email_domain_id = dom.id
    session.add(email)
    await session.commit()
    await session.refresh(email)
    return email


def _ev(pmid: str, kind: str, ts=T0, detail=None) -> DeliveryEvent:
    return DeliveryEvent(
        kind=kind, provider_message_id=pmid, occurred_at=ts,
        detail=detail if detail is not None else {},
    )


async def test_delivery_inserts_event_and_advances_status(async_session):
    email = await _mk_email(async_session, status="sent")
    fanout = AsyncMock(return_value=1)
    res = await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "delivered", detail={"smtp_response": "250"}),
        fanout=fanout,
    )
    await async_session.commit()
    assert res.inserted and res.status_changed
    await async_session.refresh(email)
    assert email.status == "delivered"
    fanout.assert_awaited_once()
    assert fanout.await_args.kwargs["event_type"] == "email.delivered"


async def test_duplicate_event_skips_fanout(async_session):
    email = await _mk_email(async_session, status="sent")
    fanout = AsyncMock(return_value=1)
    ev = _ev(email.provider_message_id, "delivered")
    await apply_delivery_event(async_session, ev, fanout=fanout)
    await async_session.commit()
    res2 = await apply_delivery_event(async_session, ev, fanout=fanout)
    await async_session.commit()
    assert not res2.inserted
    assert fanout.await_count == 1
    rows = (await async_session.execute(select(EmailEvent))).scalars().all()
    assert len(rows) == 1


async def test_soft_bounce_records_event_without_status_change(async_session):
    email = await _mk_email(async_session, status="delivered")
    fanout = AsyncMock(return_value=0)
    res = await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "bounced",
            detail={"bounce_type": "Transient"}),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert res.inserted and not res.status_changed
    assert email.status == "delivered"


async def test_hard_bounce_overrides_delivered_but_not_complained(async_session):
    email = await _mk_email(async_session, status="delivered")
    fanout = AsyncMock(return_value=0)
    await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "bounced",
            detail={"bounce_type": "Permanent"}),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "bounced"

    # complaint still wins over bounced
    await apply_delivery_event(
        async_session, _ev(email.provider_message_id, "complained"), fanout=fanout
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "complained"

    # late delivered never regresses a terminal state
    await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "delivered",
            ts=datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc)),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "complained"


async def test_rejected_sets_failed_with_end_reason_and_no_fanout(async_session):
    email = await _mk_email(async_session, status="sent")
    fanout = AsyncMock(return_value=0)
    await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "rejected", detail={"reason": "Bad content"}),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "failed"
    assert email.end_reason == "Bad content"
    assert email.failed_at is not None
    fanout.assert_not_awaited()


async def test_unmatched_provider_message_id_is_noop(async_session):
    fanout = AsyncMock()
    res = await apply_delivery_event(
        async_session, _ev("pmid-does-not-exist", "delivered"), fanout=fanout
    )
    assert res.email_id is None and not res.inserted
    fanout.assert_not_awaited()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_email_delivery_events.py -v`
Expected: FAIL with `ModuleNotFoundError: … email_delivery_events`.

- [ ] **Step 3: Implement** (`core/hailhq/core/email_delivery_events.py`)

```python
"""Apply one SES delivery event: dedup insert, status transition, fanout.

Transaction discipline: this function flushes but never commits — the
caller (the /internal/ses-events route) owns the transaction so the event
row, the status change, and the webhook delivery rows land atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import Email, EmailEvent
from hailhq.core.providers.email.inbound.ses_delivery import DeliveryEvent

__all__ = ["ApplyResult", "apply_delivery_event", "build_delivery_event_data"]

# kind → statuses it may transition FROM (guarded UPDATE … WHERE status IN).
_STATUS_FROM: dict[str, tuple[str, ...]] = {
    "delivered": ("sent",),
    "bounced": ("sent", "delivered"),      # Permanent bounces only (checked below)
    "complained": ("sent", "delivered", "bounced"),
    "rejected": ("queued", "sent"),
}

# Kinds that fan out to customer webhooks as ``email.<kind>``. ``rejected``
# is a send failure (surfaces as status=failed), not a subscribable event.
_FANOUT_KINDS = frozenset(
    {"delivered", "delivery_delayed", "bounced", "complained", "opened", "clicked"}
)

FanoutFn = Callable[..., Awaitable[int]]


@dataclass(frozen=True)
class ApplyResult:
    email_id: UUID | None
    inserted: bool
    status_changed: bool


def build_delivery_event_data(email: Email, event: DeliveryEvent) -> dict[str, Any]:
    return {
        "id": str(email.id),
        "kind": event.kind,
        "occurred_at": event.occurred_at.isoformat(),
        "from_address": email.from_address,
        "to_addresses": list(email.to_addresses),
        "subject": email.subject,
        "detail": dict(event.detail),
    }


def _new_status_for(email_status: str, event: DeliveryEvent) -> str | None:
    if event.kind == "bounced" and event.detail.get("bounce_type") != "Permanent":
        return None  # soft bounce: event only
    allowed_from = _STATUS_FROM.get(event.kind)
    if allowed_from is None or email_status not in allowed_from:
        return None
    return "failed" if event.kind == "rejected" else event.kind


async def apply_delivery_event(
    db: AsyncSession,
    event: DeliveryEvent,
    *,
    fanout: FanoutFn,
) -> ApplyResult:
    email = (
        await db.execute(
            select(Email).where(
                Email.provider_message_id == event.provider_message_id,
                Email.direction == "outbound",
            )
        )
    ).scalar_one_or_none()
    if email is None:
        # Expected for mail sent outside Hail from the same SES account.
        return ApplyResult(email_id=None, inserted=False, status_changed=False)

    ins = (
        pg_insert(EmailEvent)
        .values(
            email_id=email.id,
            organization_id=email.organization_id,
            kind=event.kind,
            payload=dict(event.detail),
            occurred_at=event.occurred_at,
        )
        .on_conflict_do_nothing(constraint="email_events_dedup_uq")
        .returning(EmailEvent.id)
    )
    inserted_id = (await db.execute(ins)).scalar_one_or_none()
    if inserted_id is None:
        # SNS redelivery — everything already happened the first time.
        return ApplyResult(email_id=email.id, inserted=False, status_changed=False)

    status_changed = False
    new_status = _new_status_for(email.status, event)
    if new_status is not None:
        values: dict[str, Any] = {"status": new_status}
        if new_status == "failed":
            values["end_reason"] = event.detail.get("reason") or "Reject"
            values["failed_at"] = datetime.now(timezone.utc)
        # Guarded UPDATE re-checks status in SQL so concurrent events can't
        # double-apply (the in-memory email.status may be stale).
        result = await db.execute(
            update(Email)
            .where(Email.id == email.id, Email.status.in_(_STATUS_FROM[event.kind]))
            .values(**values)
        )
        status_changed = result.rowcount == 1

    if event.kind in _FANOUT_KINDS:
        await fanout(
            db,
            organization_id=email.organization_id,
            email_domain_id=email.email_domain_id,
            event_type=f"email.{event.kind}",
            event_id=uuid4(),
            data=build_delivery_event_data(email, event),
        )

    await db.flush()
    return ApplyResult(email_id=email.id, inserted=True, status_changed=status_changed)
```

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_email_delivery_events.py -v`
Expected: PASS (6 tests). If the `async_session` fixture name differs, mirror whatever `api/tests/test_internal_ses_events.py` uses.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/email_delivery_events.py api/tests/test_email_delivery_events.py
git commit -m "feat(core): apply SES delivery events with dedup + guarded transitions"
```

---

### Task 5: Synthetic `sent` event at send time

**Files:**

- Modify: `api/hailhq/api/routes/emails.py` (direct-send path), `core/hailhq/core/outbound_worker.py` (forward path)
- Test: extend `api/tests/test_emails_api.py`

**Interfaces:**

- Consumes: `EmailEvent` model.
- Produces: every successful send writes one `EmailEvent(kind="sent", payload={}, occurred_at=<sent_at>)` in the same commit that sets `status='sent'`.

- [ ] **Step 1: Write the failing test** — append to `api/tests/test_emails_api.py` (reuse that file's existing send-success test setup; it already stubs the email provider via the `get_email_provider` override in conftest):

```python
async def test_send_writes_synthetic_sent_event(client, async_session):
    from sqlalchemy import select
    from hailhq.core.models import EmailEvent

    _, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "hello"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    email_id = resp.json()["id"]
    events = (
        (await async_session.execute(
            select(EmailEvent).where(EmailEvent.email_id == email_id)
        )).scalars().all()
    )
    assert [e.kind for e in events] == ["sent"]
    assert events[0].occurred_at is not None
```

(Adapt the request body/headers to match the file's existing passing send test — same org/key helper, same provider stubbing. The assertion block is the new content.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_emails_api.py -k synthetic -v`
Expected: FAIL — no EmailEvent rows.

- [ ] **Step 3: Implement**

In `api/hailhq/api/routes/emails.py`, import `EmailEvent` (extend the existing `from hailhq.core.models import …` line) and, in `create_email`, add the event row to the success UPDATE block (after `.values(status="sent", …)` and before `await db.commit()`):

```python
    db.add(
        EmailEvent(
            email_id=email.id,
            organization_id=email.organization_id,
            kind="sent",
            payload={},
            occurred_at=now,
        )
    )
    await db.commit()
```

In `core/hailhq/core/outbound_worker.py`, import `EmailEvent` (extend the `from hailhq.core.models import Email, EmailAttachment` line) and, in `_send_one`, after the success block (`row.status = "sent"` / `row.provider_message_id = …` / `row.sent_at = now`), add:

```python
        session.add(
            EmailEvent(
                email_id=row.id,
                organization_id=row.organization_id,
                kind="sent",
                payload={},
                occurred_at=now,
            )
        )
```

(The caller's `session.commit()` in `tick()` persists it atomically with the status change.)

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_emails_api.py -v && cd ../core && uv run pytest -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/emails.py core/hailhq/core/outbound_worker.py api/tests/test_emails_api.py
git commit -m "feat(api): synthetic sent event row on outbound sends"
```

---

### Task 6: `/internal/ses-events` — `delivery_event` envelope branch

**Files:**

- Modify: `api/hailhq/api/routes/internal/ses_events.py`, `infra/ses-ingest-lambda/README.md` (envelope note)
- Test: `api/tests/test_internal_ses_events_delivery.py` (create)

**Interfaces:**

- Consumes: `parse_delivery_event` (Task 3), `apply_delivery_event` (Task 4), `fanout_email_event` (existing).
- Produces: `POST /internal/ses-events` accepts `{"type": "delivery_event", "event": {<raw SES event>}}` (HMAC-signed like the inbound envelope). Responses: `{"status": "applied", "email_id": ...}`, `{"status": "duplicate", ...}`, `{"status": "unmatched"}`, `{"status": "ignored"}` (untracked eventType). Envelopes **without** `"type"` keep the existing inbound behavior byte-for-byte. Delivery events do **not** require `hail_inbound_enabled` — only the HMAC secret.

- [ ] **Step 1: Write the failing test** (`api/tests/test_internal_ses_events_delivery.py`) — copy the HMAC-signing helper style from `api/tests/test_internal_ses_events.py` (it signs `body` with `settings.hail_inbound_hmac_secret` and posts with `X-Hail-Signature: sha256=<hex>`; mirror its settings monkeypatching):

```python
"""POST /internal/ses-events with the delivery_event envelope."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from hailhq.core.config import settings
from .conftest import insert_org_and_key

SECRET = "test-hmac-secret"


def _signed(body: dict) -> tuple[bytes, dict[str, str]]:
    raw = json.dumps(body, separators=(",", ":")).encode()
    sig = hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return raw, {
        "Content-Type": "application/json",
        "X-Hail-Signature": f"sha256={sig}",
    }


def _delivery_envelope(pmid: str) -> dict:
    return {
        "type": "delivery_event",
        "event": {
            "eventType": "Delivery",
            "mail": {"messageId": pmid, "timestamp": "2026-07-01T12:00:00.000Z"},
            "delivery": {
                "timestamp": "2026-07-01T12:00:03.000Z",
                "recipients": ["bob@example.com"],
                "smtpResponse": "250 OK",
            },
        },
    }


@pytest.fixture(autouse=True)
def _hmac_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", SECRET)
    # Delivery events must work with inbound DISABLED.
    monkeypatch.setattr(settings, "hail_inbound_enabled", False)


async def test_delivery_event_applies_with_inbound_disabled(
    client, async_session
):
    # Arrange: a sent outbound email (POST /emails via stubbed provider).
    _, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "hello"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    pmid = resp.json()["provider_message_id"]

    raw, headers = _signed(_delivery_envelope(pmid))
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "applied"

    detail = await client.get(
        f"/emails/{resp.json()['id']}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert detail.json()["status"] == "delivered"


async def test_duplicate_delivery_event_reports_duplicate(client, async_session):
    _, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "hello"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    pmid = resp.json()["provider_message_id"]
    raw, headers = _signed(_delivery_envelope(pmid))
    await client.post("/internal/ses-events", content=raw, headers=headers)
    r2 = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r2.json()["status"] == "duplicate"


async def test_unmatched_pmid_returns_200_unmatched(client):
    raw, headers = _signed(_delivery_envelope("pmid-not-ours"))
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 200
    assert r.json()["status"] == "unmatched"


async def test_untracked_event_type_ignored(client):
    env = _delivery_envelope("x")
    env["event"]["eventType"] = "Send"
    raw, headers = _signed(env)
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.json()["status"] == "ignored"


async def test_bad_signature_401(client):
    raw, headers = _signed(_delivery_envelope("x"))
    headers["X-Hail-Signature"] = "sha256=deadbeef"
    r = await client.post("/internal/ses-events", content=raw, headers=headers)
    assert r.status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_internal_ses_events_delivery.py -v`
Expected: FAIL — inbound-disabled requests get 503 (the current route-level dependency), and no `delivery_event` handling exists.

- [ ] **Step 3: Implement** — restructure `api/hailhq/api/routes/internal/ses_events.py`:

1. Remove `dependencies=[Depends(require_inbound_enabled)]` from the route decorator (keep the function `require_inbound_enabled` and call it inline for the inbound branch).
2. Add imports:

```python
import json

from hailhq.core.email_delivery_events import apply_delivery_event
from hailhq.core.providers.email.inbound.ses_delivery import parse_delivery_event
```

3. Rework the handler body — after signature verification, branch on the envelope:

```python
@router.post("/ses-events")
async def receive_ses_event(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SesInboundProvider, Depends(get_inbound_provider)],
    s3: Annotated[S3InboundClient, Depends(get_s3_inbound_client)],
    x_hail_signature: Annotated[str | None, Header()] = None,
) -> dict:
    body = await request.body()
    headers = {"X-Hail-Signature": x_hail_signature or ""}
    if not await provider.verify_notification(headers, body):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )

    try:
        envelope = json.loads(body)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="malformed SES notification payload",
        ) from exc

    if isinstance(envelope, dict) and envelope.get("type") == "delivery_event":
        return await _handle_delivery_event(db, envelope)

    # Legacy inbound envelope (no "type" key) — gate + flow unchanged.
    require_inbound_enabled()
    try:
        message = await provider.parse_notification(body)
    except Exception as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="malformed SES notification payload",
        ) from exc
    ...  # existing ingest_inbound flow, unchanged
```

4. Add the delivery handler in the same file:

```python
async def _handle_delivery_event(db: AsyncSession, envelope: dict) -> dict:
    raw_event = envelope.get("event")
    if not isinstance(raw_event, dict):
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="delivery_event envelope missing 'event' object",
        )
    try:
        event = parse_delivery_event(raw_event)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=http_status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="malformed SES delivery event",
        ) from exc
    if event is None:
        return {"status": "ignored"}

    result = await apply_delivery_event(db, event, fanout=fanout_email_event)
    await db.commit()
    if result.email_id is None:
        # Mail sent outside Hail from the same SES account — ack, log, count.
        logger.info(
            "unmatched delivery event pmid=%s kind=%s",
            event.provider_message_id,
            event.kind,
        )
        return {"status": "unmatched"}
    if not result.inserted:
        return {"status": "duplicate", "email_id": str(result.email_id)}
    return {"status": "applied", "email_id": str(result.email_id)}
```

Note the return-type change on the route (`dict[str, list[str]]` → `dict`); the inbound branch's return payload is unchanged.

- [ ] **Step 4: Run tests (new + all existing internal-ses suites)**

Run: `cd api && uv run pytest tests/test_internal_ses_events_delivery.py tests/test_internal_ses_events.py tests/test_internal_ses_events_multi_org.py -v`
Expected: all PASS — the legacy inbound behavior must be untouched.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/internal/ses_events.py api/tests/test_internal_ses_events_delivery.py infra/ses-ingest-lambda/README.md
git commit -m "feat(api): ingest SES delivery events via /internal/ses-events"
```

---

### Task 7: `GET /emails/{id}/events`

**Files:**

- Modify: `api/hailhq/api/routes/emails.py`
- Test: `api/tests/test_email_events_api.py` (create)

**Interfaces:**

- Consumes: `EmailEvent` model, `EmailEventResponse`/`EmailEventListResponse` (Task 2).
- Produces: `GET /emails/{email_id}/events` → `EmailEventListResponse`, events ordered by `(occurred_at, id)` ascending, org-scoped, 404 on unknown/cross-org email. No pagination (bounded: one email's events). Also: `EmailResponse.last_event_at: datetime | None` (schema field, `None` default), populated on `GET /emails/{id}` from `max(email_events.occurred_at)`.

- [ ] **Step 1: Write the failing test** (`api/tests/test_email_events_api.py`)

```python
"""GET /emails/{id}/events — per-email lifecycle timeline."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailEvent
from .conftest import insert_org_and_key


async def _send_email(client, plain: str) -> dict:
    resp = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "hello"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    return resp.json()


async def test_events_ordered_and_scoped(client, async_session: AsyncSession):
    org_id, _, plain = await insert_org_and_key(async_session)
    email = await _send_email(client, plain)

    async_session.add(
        EmailEvent(
            email_id=email["id"],
            organization_id=org_id,
            kind="delivered",
            payload={"smtp_response": "250 OK"},
            occurred_at=datetime(2026, 7, 1, 12, 0, 3, tzinfo=timezone.utc),
        )
    )
    await async_session.commit()

    r = await client.get(
        f"/emails/{email['id']}/events",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200
    kinds = [e["kind"] for e in r.json()["items"]]
    assert kinds == ["sent", "delivered"] or kinds == ["delivered", "sent"]
    # ascending by occurred_at:
    times = [e["occurred_at"] for e in r.json()["items"]]
    assert times == sorted(times)

    # cross-org → 404
    _, _, other_plain = await insert_org_and_key(async_session)
    r2 = await client.get(
        f"/emails/{email['id']}/events",
        headers={"Authorization": f"Bearer {other_plain}"},
    )
    assert r2.status_code == 404


async def test_get_email_exposes_last_event_at(client, async_session):
    _, _, plain = await insert_org_and_key(async_session)
    email = await _send_email(client, plain)
    r = await client.get(
        f"/emails/{email['id']}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200
    # The synthetic 'sent' event exists, so last_event_at is populated.
    assert r.json()["last_event_at"] is not None
```

(Note: the synthetic `sent` event's `occurred_at` is set at send time; the injected `delivered` event may sort before or after it depending on test clock — the ordering assertion is on the returned list being ascending, which is the contract.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_email_events_api.py -v`
Expected: FAIL with 404 (route doesn't exist — FastAPI matches `/{email_id}` then fails the trailing `/events` → 404).

- [ ] **Step 3: Implement** — in `api/hailhq/api/routes/emails.py`, import `EmailEvent`, `EmailEventListResponse`, `EmailEventResponse`, and add **above** the `GET /{email_id}` route (route order matters for Task 8; keep the two new GETs together):

```python
@router.get("/{email_id}/events", response_model=EmailEventListResponse)
async def list_email_events(
    email_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> EmailEventListResponse:
    """Chronological lifecycle events for one email (org-scoped)."""
    exists = (
        await db.execute(
            select(Email.id).where(
                Email.id == email_id,
                Email.organization_id == principal.organization_id,
            )
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="email not found",
        )
    rows = (
        (
            await db.execute(
                select(EmailEvent)
                .where(EmailEvent.email_id == email_id)
                .order_by(EmailEvent.occurred_at.asc(), EmailEvent.id.asc())
            )
        )
        .scalars()
        .all()
    )
    return EmailEventListResponse(
        items=[EmailEventResponse.model_validate(e) for e in rows]
    )
```

Then wire `last_event_at`:

1. In `core/hailhq/core/schemas.py`, add to `EmailResponse` (next to the other outbound-shape defaults): `last_event_at: datetime | None = None`.
2. In the existing `get_email` handler (`GET /{email_id}`), after `resp = EmailResponse.model_validate(email)`:

```python
    resp.last_event_at = (
        await db.execute(
            select(func.max(EmailEvent.occurred_at)).where(
                EmailEvent.email_id == email.id
            )
        )
    ).scalar_one_or_none()
```

(add `from sqlalchemy import func` to the route file's imports).

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_email_events_api.py tests/test_emails_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/emails.py api/tests/test_email_events_api.py
git commit -m "feat(api): GET /emails/{id}/events timeline endpoint"
```

---

### Task 8: `GET /emails/stats`

**Files:**

- Modify: `core/hailhq/core/schemas.py` (stats response models), `api/hailhq/api/routes/emails.py`
- Test: `api/tests/test_email_stats_api.py` (create)

**Interfaces:**

- Produces (schemas):

```python
class EmailStatsCounts(BaseModel):
    sent: int = 0
    delivered: int = 0
    delivery_delayed: int = 0
    bounced: int = 0
    bounced_hard: int = 0
    complained: int = 0
    rejected: int = 0
    opened: int = 0
    clicked: int = 0
    unique_opened: int = 0
    unique_clicked: int = 0


class EmailStatsBucket(EmailStatsCounts):
    bucket_start: datetime


class EmailStatsRates(BaseModel):
    """All None when sent == 0 in the window."""
    delivery: float | None = None
    bounce: float | None = None       # hard bounces / sent
    complaint: float | None = None
    open: float | None = None         # unique_opened / sent
    click: float | None = None        # unique_clicked / sent


class EmailStatsResponse(BaseModel):
    from_ts: datetime = Field(serialization_alias="from")
    to_ts: datetime = Field(serialization_alias="to")
    bucket: Literal["hour", "day"]
    totals: EmailStatsCounts
    rates: EmailStatsRates
    series: list[EmailStatsBucket]
```

- Produces (route): `GET /emails/stats?from=&to=&bucket=hour|day`. Defaults: `to=now`, `from=to-7d`, `bucket=day`. 422 when `from >= to`, range > 92 days, or `bucket=hour` with range > 8 days. Buckets are UTC `date_trunc` boundaries; zero-filled across the range.

- [ ] **Step 1: Write the failing test** (`api/tests/test_email_stats_api.py`)

```python
"""GET /emails/stats — account-level deliverability aggregates."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailEvent
from .conftest import insert_org_and_key

DAY1 = datetime(2026, 6, 28, 10, 0, tzinfo=timezone.utc)
DAY2 = datetime(2026, 6, 29, 11, 0, tzinfo=timezone.utc)


async def _seed(session: AsyncSession, org_id, email_id, kind, ts, payload=None):
    session.add(EmailEvent(
        email_id=email_id, organization_id=org_id, kind=kind,
        payload=payload or {}, occurred_at=ts,
    ))


async def _sent_email_id(client, plain) -> str:
    r = await client.post(
        "/emails",
        json={"to": ["b@example.com"], "subject": "s", "body_text": "t"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def test_stats_totals_series_and_rates(client, async_session):
    org_id, _, plain = await insert_org_and_key(async_session)
    e1 = await _sent_email_id(client, plain)
    e2 = await _sent_email_id(client, plain)
    # Re-stamp the two synthetic sent events into the window under test.
    from sqlalchemy import update
    from hailhq.core.models import EmailEvent as EE
    await async_session.execute(
        update(EE).where(EE.email_id == e1).values(occurred_at=DAY1))
    await async_session.execute(
        update(EE).where(EE.email_id == e2).values(occurred_at=DAY2))

    await _seed(async_session, org_id, e1, "delivered", DAY1)
    await _seed(async_session, org_id, e1, "opened", DAY1)
    await _seed(async_session, org_id, e1, "opened", DAY2)  # repeat open
    await _seed(async_session, org_id, e2, "bounced", DAY2,
                {"bounce_type": "Permanent"})
    await async_session.commit()

    r = await client.get(
        "/emails/stats",
        params={"from": "2026-06-28T00:00:00Z", "to": "2026-06-30T00:00:00Z",
                "bucket": "day"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200
    body = r.json()
    t = body["totals"]
    assert t["sent"] == 2 and t["delivered"] == 1
    assert t["bounced"] == 1 and t["bounced_hard"] == 1
    assert t["opened"] == 2 and t["unique_opened"] == 1
    assert body["rates"]["delivery"] == 0.5
    assert body["rates"]["bounce"] == 0.5
    assert body["rates"]["open"] == 0.5
    assert len(body["series"]) == 2  # two zero-filled day buckets
    assert body["series"][0]["sent"] == 1


async def test_stats_zero_sends_null_rates(client, async_session):
    _, _, plain = await insert_org_and_key(async_session)
    r = await client.get(
        "/emails/stats", headers={"Authorization": f"Bearer {plain}"}
    )
    assert r.status_code == 200
    assert r.json()["rates"]["delivery"] is None


async def test_stats_bounds_validation(client, async_session):
    _, _, plain = await insert_org_and_key(async_session)
    h = {"Authorization": f"Bearer {plain}"}
    # from >= to
    r = await client.get("/emails/stats", params={
        "from": "2026-06-30T00:00:00Z", "to": "2026-06-28T00:00:00Z"}, headers=h)
    assert r.status_code == 422
    # > 92 days
    r = await client.get("/emails/stats", params={
        "from": "2026-01-01T00:00:00Z", "to": "2026-06-01T00:00:00Z"}, headers=h)
    assert r.status_code == 422
    # hour bucket on > 8 days
    r = await client.get("/emails/stats", params={
        "from": "2026-06-01T00:00:00Z", "to": "2026-06-20T00:00:00Z",
        "bucket": "hour"}, headers=h)
    assert r.status_code == 422


async def test_stats_scoped_to_org(client, async_session):
    org_a, _, plain_a = await insert_org_and_key(async_session)
    _, _, plain_b = await insert_org_and_key(async_session)
    e1 = await _sent_email_id(client, plain_a)
    await async_session.commit()
    r = await client.get(
        "/emails/stats", headers={"Authorization": f"Bearer {plain_b}"}
    )
    assert r.json()["totals"]["sent"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_email_stats_api.py -v`
Expected: FAIL — `/emails/stats` currently matches `GET /emails/{email_id}` and 422s on UUID parsing.

- [ ] **Step 3: Add the stats schemas to `core/hailhq/core/schemas.py`** (code in Interfaces above, placed after `EmailEventListResponse`; `EmailStatsResponse` needs `model_config = ConfigDict(populate_by_name=True)` so `from_ts`/`to_ts` serialize as `from`/`to`).

- [ ] **Step 4: Implement the route** — in `api/hailhq/api/routes/emails.py`, **above** `GET /{email_id}` (FastAPI matches in registration order; `stats` must not be swallowed by the UUID path param):

```python
_STATS_MAX_RANGE_DAYS = 92
_STATS_MAX_HOURLY_DAYS = 8
_STATS_COUNT_KINDS = (
    "sent", "delivered", "delivery_delayed", "bounced",
    "complained", "rejected", "opened", "clicked",
)

_STATS_SQL = text_sql(
    """
    SELECT
      date_trunc(:bucket, occurred_at) AS bucket_start,
      kind,
      count(*) AS total,
      count(DISTINCT email_id) AS unique_emails,
      count(*) FILTER (WHERE payload->>'bounce_type' = 'Permanent') AS hard
    FROM email_events
    WHERE organization_id = :org
      AND occurred_at >= :from_ts
      AND occurred_at < :to_ts
    GROUP BY 1, 2
    """
)


@router.get("/stats", response_model=EmailStatsResponse)
async def get_email_stats(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: Annotated[datetime | None, Query()] = None,
    bucket: Literal["hour", "day"] = Query(default="day"),
) -> EmailStatsResponse:
    to_ts = to or datetime.now(timezone.utc)
    from_ts = from_ or to_ts - timedelta(days=7)
    if from_ts.tzinfo is None or to_ts.tzinfo is None:
        raise HTTPException(422, detail="from/to must be timezone-aware ISO 8601")
    if from_ts >= to_ts:
        raise HTTPException(422, detail="'from' must be before 'to'")
    span = to_ts - from_ts
    if span > timedelta(days=_STATS_MAX_RANGE_DAYS):
        raise HTTPException(422, detail=f"range exceeds {_STATS_MAX_RANGE_DAYS} days")
    if bucket == "hour" and span > timedelta(days=_STATS_MAX_HOURLY_DAYS):
        raise HTTPException(
            422, detail=f"bucket=hour limited to {_STATS_MAX_HOURLY_DAYS} days"
        )

    rows = (
        await db.execute(
            _STATS_SQL,
            {
                "bucket": bucket,
                "org": principal.organization_id,
                "from_ts": from_ts,
                "to_ts": to_ts,
            },
        )
    ).all()

    step = timedelta(hours=1) if bucket == "hour" else timedelta(days=1)
    start = _truncate(from_ts, bucket)
    buckets: dict[datetime, EmailStatsBucket] = {}
    cur = start
    while cur < to_ts:
        buckets[cur] = EmailStatsBucket(bucket_start=cur)
        cur += step

    totals = EmailStatsCounts()
    for bucket_start, kind, total, unique_emails, hard in rows:
        b = buckets.get(bucket_start)
        if b is None:  # bucket before the truncated start edge
            continue
        setattr(b, kind, getattr(b, kind) + total)
        setattr(totals, kind, getattr(totals, kind) + total)
        if kind == "opened":
            b.unique_opened += unique_emails
            totals.unique_opened += unique_emails
        elif kind == "clicked":
            b.unique_clicked += unique_emails
            totals.unique_clicked += unique_emails
        elif kind == "bounced":
            b.bounced_hard += hard
            totals.bounced_hard += hard

    rates = EmailStatsRates()
    if totals.sent:
        rates.delivery = totals.delivered / totals.sent
        rates.bounce = totals.bounced_hard / totals.sent
        rates.complaint = totals.complained / totals.sent
        rates.open = totals.unique_opened / totals.sent
        rates.click = totals.unique_clicked / totals.sent

    return EmailStatsResponse(
        from_ts=from_ts,
        to_ts=to_ts,
        bucket=bucket,
        totals=totals,
        rates=rates,
        series=[buckets[k] for k in sorted(buckets)],
    )


def _truncate(ts: datetime, bucket: str) -> datetime:
    if bucket == "hour":
        return ts.replace(minute=0, second=0, microsecond=0)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)
```

Imports to add in the route file: `from datetime import timedelta`, `from sqlalchemy import text as text_sql`, plus `EmailStatsBucket, EmailStatsCounts, EmailStatsRates, EmailStatsResponse` from `hailhq.core.schemas`.

Caveat for the implementer: `totals.unique_opened` summed across buckets over-counts an email opened in two different buckets at the **totals** level. Fix inside this task by computing window-level uniques with a second small query:

```python
_STATS_UNIQUE_SQL = text_sql(
    """
    SELECT kind, count(DISTINCT email_id) AS uniq
    FROM email_events
    WHERE organization_id = :org
      AND occurred_at >= :from_ts AND occurred_at < :to_ts
      AND kind IN ('opened', 'clicked')
    GROUP BY kind
    """
)
```

and overwrite `totals.unique_opened` / `totals.unique_clicked` from its result (per-bucket uniques stay per-bucket).

- [ ] **Step 5: Run tests**

Run: `cd api && uv run pytest tests/test_email_stats_api.py tests/test_emails_api.py -v`
Expected: PASS, including the pre-existing emails suite (route-order regression check).

- [ ] **Step 6: Commit**

```bash
git add core/hailhq/core/schemas.py api/hailhq/api/routes/emails.py api/tests/test_email_stats_api.py
git commit -m "feat(api): GET /emails/stats deliverability aggregates"
```

---

### Task 9: Email events join `GET /events`

**Files:**

- Modify: `api/hailhq/api/routes/events.py`
- Test: extend `api/tests/test_events_api.py`

**Interfaces:**

- Consumes: `EventResponse` (Task 2), `EmailEvent` model.
- Produces: `GET /events` returns a merged, `(occurred_at, id)`-ordered stream of call + email events. Items carry `source` (`"call"`/`"email"`) and exactly one of `call_id`/`email_id`. `id=email:<uuid>` filters to one email (404 unknown/cross-org). `call_status` populated only for `id=call:...` — unchanged.

- [ ] **Step 1: Write the failing test** — append to `api/tests/test_events_api.py`:

```python
async def test_stream_merges_email_events(client, async_session):
    from hailhq.core.models import EmailEvent

    org_id, _, plain = await insert_org_and_key(async_session)
    call_id = await _create_call_for_events(client, plain)
    await _add_event(async_session, call_id, "call.queued", {})

    resp = await client.post(
        "/emails",
        json={"to": ["b@example.com"], "subject": "s", "body_text": "t"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    email_id = resp.json()["id"]

    r = await client.get("/events", headers={"Authorization": f"Bearer {plain}"})
    assert r.status_code == 200
    items = r.json()["items"]
    sources = {i["source"] for i in items}
    assert sources == {"call", "email"}
    email_items = [i for i in items if i["source"] == "email"]
    assert email_items[0]["email_id"] == email_id
    assert email_items[0]["call_id"] is None
    # ascending (occurred_at, id) across both sources
    keys = [(i["occurred_at"], i["id"]) for i in items]
    assert keys == sorted(keys)


async def test_stream_email_id_filter(client, async_session):
    org_id, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/emails",
        json={"to": ["b@example.com"], "subject": "s", "body_text": "t"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    email_id = resp.json()["id"]
    r = await client.get(
        "/events", params={"id": f"email:{email_id}"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200
    assert all(i["email_id"] == email_id for i in r.json()["items"])

    # cross-org 404
    _, _, other = await insert_org_and_key(async_session)
    r2 = await client.get(
        "/events", params={"id": f"email:{email_id}"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert r2.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd api && uv run pytest tests/test_events_api.py -v`
Expected: new tests FAIL (`source` missing / `email:` rejected as unsupported type — Task 2 already added it to `SUPPORTED_RESOURCE_TYPES`, so the 422 path passes but the query returns no email rows).

- [ ] **Step 3: Implement** — rewrite the query in `api/hailhq/api/routes/events.py` as a UNION ALL over per-source selects with aligned columns, then order/paginate the union. Replace the body of `list_events` from the `stmt` construction down:

```python
from sqlalchemy import literal, select, tuple_, union_all

from hailhq.core.models import Call, CallEvent, Email, EmailEvent
from hailhq.core.schemas import (
    CallStatus,
    EventResponse,
    EventStreamResponse,
    decode_cursor,
    encode_cursor,
    parse_resource_id,
)


def _call_select():
    return select(
        CallEvent.id.label("id"),
        literal("call").label("source"),
        CallEvent.call_id.label("call_id"),
        literal(None).label("email_id"),
        CallEvent.kind.label("kind"),
        CallEvent.payload.label("payload"),
        CallEvent.occurred_at.label("occurred_at"),
    )


def _email_select():
    return select(
        EmailEvent.id.label("id"),
        literal("email").label("source"),
        literal(None).label("call_id"),
        EmailEvent.email_id.label("email_id"),
        EmailEvent.kind.label("kind"),
        EmailEvent.payload.label("payload"),
        EmailEvent.occurred_at.label("occurred_at"),
    )
```

Inside the handler:

```python
    call_status = None
    selects = []
    if resource_type == "call":
        # existing 404-checked lookup, then:
        selects = [_call_select().where(CallEvent.call_id == resource_uuid)]
        call_status = call.status
    elif resource_type == "email":
        email_row = (
            await db.execute(
                select(Email.id).where(
                    Email.id == resource_uuid,
                    Email.organization_id == principal.organization_id,
                )
            )
        ).scalar_one_or_none()
        if email_row is None:
            raise HTTPException(
                status_code=http_status.HTTP_404_NOT_FOUND,
                detail="email not found",
            )
        selects = [_email_select().where(EmailEvent.email_id == resource_uuid)]
    else:
        selects = [
            _call_select()
            .join(Call, Call.id == CallEvent.call_id)
            .where(Call.organization_id == principal.organization_id),
            _email_select().where(
                EmailEvent.organization_id == principal.organization_id
            ),
        ]

    if kind is not None:
        selects = [s.where(s.selected_columns.kind == kind) for s in selects]

    u = union_all(*selects).subquery() if len(selects) > 1 else selects[0].subquery()
    stmt = select(u)
    if cursor is not None:
        # decode_cursor + 400 handling unchanged
        stmt = stmt.where(tuple_(u.c.occurred_at, u.c.id) > tuple_(cur_ts, cur_id))
    stmt = stmt.order_by(u.c.occurred_at.asc(), u.c.id.asc()).limit(limit + 1)
    rows = (await db.execute(stmt)).all()

    next_cursor = None
    if len(rows) > limit:
        last = rows[limit - 1]
        next_cursor = encode_cursor(last.occurred_at, last.id)
        rows = rows[:limit]

    return EventStreamResponse(
        items=[EventResponse.model_validate(r, from_attributes=True) for r in rows],
        next_cursor=next_cursor,
        call_status=cast(CallStatus | None, call_status),
    )
```

Kind filtering caveat: applying `.where(s.selected_columns.kind == kind)` before the union keeps the filter sargable per-table. If `selected_columns` access is awkward on the joined call select, filter on `CallEvent.kind` / `EmailEvent.kind` directly when building each select instead.

- [ ] **Step 4: Run tests**

Run: `cd api && uv run pytest tests/test_events_api.py -v`
Expected: ALL pass — pre-existing call-stream tests are the back-compat gate (`call_id`, `kind`, cursor behavior unchanged; items gain `source`/`email_id` fields).

- [ ] **Step 5: Commit**

```bash
git add api/hailhq/api/routes/events.py api/tests/test_events_api.py
git commit -m "feat(api): email events join the unified GET /events stream"
```

---

### Task 10: SES configuration set on sends

**Files:**

- Modify: `core/hailhq/core/config.py`, `core/hailhq/core/providers/email/ses.py`, `.env.example`
- Test: `core/tests/test_ses_provider.py` (extend the existing SES provider test file; find it with `ls core/tests | grep -i ses` — if the provider tests live elsewhere, extend there)

**Interfaces:**

- Produces: `settings.hail_ses_configuration_set: str = ""`; when non-empty, every `SesEmailProvider.send_email()` call (Simple and Raw paths) includes `ConfigurationSetName`.

- [ ] **Step 1: Write the failing test** — Stubber-based, matching the existing provider test style:

```python
async def test_send_email_attaches_configuration_set(monkeypatch):
    import boto3
    from botocore.stub import ANY, Stubber

    from hailhq.core.config import settings
    from hailhq.core.providers.email.ses import SesEmailProvider

    monkeypatch.setattr(settings, "hail_ses_configuration_set", "hail-events")
    client = boto3.client("sesv2", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "send_email",
        {"MessageId": "mid-123"},
        {
            "FromEmailAddress": "a@b.com",
            "Destination": {"ToAddresses": ["c@d.com"]},
            "Content": ANY,
            "ConfigurationSetName": "hail-events",
        },
    )
    with stubber:
        provider = SesEmailProvider(client=client)
        result = await provider.send_email(
            from_address="a@b.com", to_addresses=["c@d.com"],
            subject="s", body_text="t", body_html=None,
        )
    assert result.provider_message_id == "mid-123"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd core && uv run pytest tests/ -k configuration_set -v`
Expected: FAIL — Stubber raises on the missing `ConfigurationSetName` param.

- [ ] **Step 3: Implement**

`core/hailhq/core/config.py` — after `hail_mail_default_user_prefix`:

```python
    # SES configuration set attached to every outbound send. Provisioned by
    # infra/terraform (event destinations → SNS → ingest Lambda → API); the
    # name must match the Terraform variable. Empty = sends carry no config
    # set and no delivery/engagement events are published.
    hail_ses_configuration_set: str = ""
```

`core/hailhq/core/providers/email/ses.py` — in `send_email`, immediately before **each** of the two `await asyncio.to_thread(self._client.send_email, **kwargs)` calls (Raw and Simple paths):

```python
        if settings.hail_ses_configuration_set:
            kwargs["ConfigurationSetName"] = settings.hail_ses_configuration_set
```

`.env.example` — add under the AWS/SES section:

```bash
# SES configuration set for delivery/engagement event tracking (must match
# the Terraform-provisioned set; empty disables event publishing)
HAIL_SES_CONFIGURATION_SET=
```

- [ ] **Step 4: Run tests**

Run: `cd core && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add core/hailhq/core/config.py core/hailhq/core/providers/email/ses.py core/tests/ .env.example
git commit -m "feat(core): attach SES configuration set to outbound sends"
```

---

### Task 11: Lambda — relay SNS delivery events

**Files:**

- Modify: `infra/ses-ingest-lambda/handler.py`, `infra/ses-ingest-lambda/test_handler.py`

**Interfaces:**

- Produces: the Lambda handles a second event source. SNS records (`Records[0].Sns.Message` = raw SES event JSON string) are wrapped as `{"type": "delivery_event", "event": <parsed message>}` and POSTed to the same `/internal/ses-events` with the same HMAC scheme. SES receipt-rule records keep the existing inbound payload (no `type` key).

- [ ] **Step 1: Write the failing test** — add to `infra/ses-ingest-lambda/test_handler.py`, following its existing monkeypatched-urlopen pattern:

```python
def test_sns_delivery_event_wrapped_and_signed(monkeypatch, captured_request):
    # `captured_request` = however the existing tests capture the urlopen
    # call (fixture or monkeypatch helper); reuse it verbatim.
    ses_event = {
        "eventType": "Delivery",
        "mail": {"messageId": "mid-1", "timestamp": "2026-07-01T12:00:00.000Z"},
        "delivery": {"timestamp": "2026-07-01T12:00:03.000Z",
                     "recipients": ["b@example.com"]},
    }
    sns_record = {"Sns": {"Message": json.dumps(ses_event)}}
    handler.handler({"Records": [sns_record]}, None)

    body = json.loads(captured_request.data)
    assert body["type"] == "delivery_event"
    assert body["event"]["eventType"] == "Delivery"
    sig = captured_request.headers["X-hail-signature"]
    assert sig.startswith("sha256=")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd infra/ses-ingest-lambda && python -m pytest test_handler.py -v` (check the README/existing CI for the exact invocation — the file is stdlib+pytest)
Expected: FAIL with `KeyError: 'ses'`.

- [ ] **Step 3: Implement** — in `handler.py`, replace the top of `handler()`:

```python
def handler(event: dict, _context) -> dict:
    record = event["Records"][0]
    if "Sns" in record:
        # Config-set delivery/engagement event relayed through SNS.
        payload = {
            "type": "delivery_event",
            "event": json.loads(record["Sns"]["Message"]),
        }
    else:
        payload = _make_payload(record["ses"])
    body = json.dumps(payload, separators=(",", ":")).encode()
    # …signing + POST unchanged below…
```

- [ ] **Step 4: Run tests**

Run: `cd infra/ses-ingest-lambda && python -m pytest test_handler.py -v`
Expected: PASS (existing inbound tests + new SNS test).

- [ ] **Step 5: Commit**

```bash
git add infra/ses-ingest-lambda/handler.py infra/ses-ingest-lambda/test_handler.py
git commit -m "feat(infra): lambda relays SNS delivery events to the API"
```

---

### Task 12: Terraform — configuration set, SNS topic, subscriptions, DLQ

**Files:**

- Create: `infra/terraform/ses_events.tf`
- Modify: `infra/terraform/variables.tf`, `infra/terraform/outputs.tf`

**Interfaces:**

- Produces: `aws_sesv2_configuration_set.events` (name from `var.ses_configuration_set_name`, default `"hail-events"`), event destination publishing `DELIVERY, BOUNCE, COMPLAINT, REJECT, DELIVERY_DELAY, OPEN, CLICK` to `aws_sns_topic.ses_events`, SNS→Lambda subscription reusing the existing ingest Lambda, Lambda invoke permission for SNS, and an SQS DLQ on the subscription.

- [ ] **Step 1: Read the existing module first** — `infra/terraform/lambda_ingest.tf` and `variables.tf` define the Lambda resource name and naming conventions (`${var.name_prefix}-…`). Use the actual Lambda resource address found there in the code below (placeholder `aws_lambda_function.ingest`).

- [ ] **Step 2: Write `infra/terraform/ses_events.tf`**

```hcl
# SES configuration set → SNS → ingest Lambda → POST /internal/ses-events.
# Open/Click tracking uses the default SES tracking domain in v1; a custom
# tracking domain is a fast-follow (see the deliverability design spec).

resource "aws_sesv2_configuration_set" "events" {
  configuration_set_name = var.ses_configuration_set_name
}

resource "aws_sns_topic" "ses_events" {
  name = "${var.name_prefix}-ses-events"
}

resource "aws_sesv2_configuration_set_event_destination" "sns" {
  configuration_set_name = aws_sesv2_configuration_set.events.configuration_set_name
  event_destination_name = "sns"

  event_destination {
    enabled = true
    matching_event_types = [
      "DELIVERY", "BOUNCE", "COMPLAINT", "REJECT",
      "DELIVERY_DELAY", "OPEN", "CLICK",
    ]
    sns_destination {
      topic_arn = aws_sns_topic.ses_events.arn
    }
  }
}

resource "aws_sqs_queue" "ses_events_dlq" {
  name                      = "${var.name_prefix}-ses-events-dlq"
  message_retention_seconds = 1209600 # 14 days
}

resource "aws_sqs_queue_policy" "ses_events_dlq" {
  queue_url = aws_sqs_queue.ses_events_dlq.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "sns.amazonaws.com" }
      Action    = "sqs:SendMessage"
      Resource  = aws_sqs_queue.ses_events_dlq.arn
      Condition = {
        ArnEquals = { "aws:SourceArn" = aws_sns_topic.ses_events.arn }
      }
    }]
  })
}

resource "aws_sns_topic_subscription" "ses_events_lambda" {
  topic_arn = aws_sns_topic.ses_events.arn
  protocol  = "lambda"
  endpoint  = aws_lambda_function.ingest.arn # match the real resource address

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.ses_events_dlq.arn
  })
}

resource "aws_lambda_permission" "sns_ses_events" {
  statement_id  = "AllowSNSSesEvents"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.ingest.function_name
  principal     = "sns.amazonaws.com"
  source_arn    = aws_sns_topic.ses_events.arn
}
```

`variables.tf` addition:

```hcl
variable "ses_configuration_set_name" {
  description = "SES configuration set attached to outbound sends (must match HAIL_SES_CONFIGURATION_SET in the API env)."
  type        = string
  default     = "hail-events"
}
```

`outputs.tf` addition:

```hcl
output "ses_configuration_set_name" {
  value = aws_sesv2_configuration_set.events.configuration_set_name
}
```

- [ ] **Step 3: Validate**

Run: `cd infra/terraform && terraform init -backend=false && terraform validate`
Expected: `Success! The configuration is valid.` (Plan/apply is a manual operator step — do not apply from this task.)

- [ ] **Step 4: Commit**

```bash
git add infra/terraform/ses_events.tf infra/terraform/variables.tf infra/terraform/outputs.tf
git commit -m "feat(infra): SES configuration set + SNS event pipeline"
```

---

### Task 13: OpenAPI regen + CLI (`hail email events`, `hail email stats`)

**Files:**

- Modify: `openapi/openapi.yaml` (regenerated), CLI generated client (regen per `cli/` README/Makefile), `cli/internal/cmd/email.go`
- Create: `cli/internal/cmd/email_events.go`, `cli/internal/cmd/email_events_test.go`, `cli/internal/cmd/email_stats.go`, `cli/internal/cmd/email_stats_test.go`

**Interfaces:**

- Consumes: the two new endpoints + `EventResponse` stream shape from the regenerated spec.
- Produces: `hail email events <id>` (table: KIND, OCCURRED AT, DETAIL) and `hail email stats [--from --to --bucket]` (totals + rates block).

- [ ] **Step 1: Regenerate the OpenAPI spec** (API must be running locally: `cd api && uv run uvicorn hailhq.api.main:app --port 8080` in another shell, DB up):

```bash
curl -s http://localhost:8080/openapi.json \
  | python -c "import json, sys, yaml; yaml.safe_dump(json.load(sys.stdin), sys.stdout, sort_keys=False)" \
  > openapi/openapi.yaml
git diff --stat openapi/openapi.yaml   # sanity: new paths /emails/stats, /emails/{email_id}/events
```

- [ ] **Step 2: Regenerate the Go client** — find the codegen entrypoint (`grep -rn "generate" cli/Makefile cli/README.md cli/internal/client/*.go | head`; there is a `go:generate` or make target that consumes `openapi/openapi.yaml`). Run it, then:

Run: `cd cli && go build ./...`
Expected: compiles. If `tail.go`/`email_tail.go` reference removed/renamed event fields, update them to read the new optional `Source`/`EmailId` fields (additive change; `CallId` remains).

- [ ] **Step 3: Write failing CLI tests** — mirror `cli/internal/cmd/email_list_test.go`'s httptest-server pattern:

`cli/internal/cmd/email_events_test.go`:

```go
package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEmailEventsRendersTimeline(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if !strings.HasSuffix(r.URL.Path, "/events") {
			t.Fatalf("unexpected path %s", r.URL.Path)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"items":[
			{"id":"e1","email_id":"m1","kind":"sent","payload":{},"occurred_at":"2026-07-01T12:00:00Z"},
			{"id":"e2","email_id":"m1","kind":"delivered","payload":{"smtp_response":"250 OK"},"occurred_at":"2026-07-01T12:00:03Z"}
		]}`))
	}))
	defer srv.Close()

	out, err := runCLIForTest(t, srv.URL, "email", "events", "11111111-1111-1111-1111-111111111111")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"sent", "delivered", "2026-07-01T12:00:03Z"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
}
```

`cli/internal/cmd/email_stats_test.go`:

```go
package cmd

import (
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestEmailStatsRendersTotalsAndRates(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = w.Write([]byte(`{"from":"2026-06-24T00:00:00Z","to":"2026-07-01T00:00:00Z",
			"bucket":"day",
			"totals":{"sent":100,"delivered":97,"delivery_delayed":1,"bounced":3,
				"bounced_hard":2,"complained":0,"rejected":0,"opened":40,"clicked":12,
				"unique_opened":35,"unique_clicked":10},
			"rates":{"delivery":0.97,"bounce":0.02,"complaint":0.0,"open":0.35,"click":0.10},
			"series":[]}`))
	}))
	defer srv.Close()

	out, err := runCLIForTest(t, srv.URL, "email", "stats")
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []string{"sent", "100", "97.0%", "2.0%"} {
		if !strings.Contains(out, want) {
			t.Errorf("output missing %q:\n%s", want, out)
		}
	}
}
```

(`runCLIForTest` = whatever helper the existing `email_list_test.go` uses to run a command against a base URL; reuse it exactly. If it has a different name, use that name in both tests.)

- [ ] **Step 4: Run tests to verify they fail**

Run: `cd cli && go test ./internal/cmd/ -run 'TestEmailEvents|TestEmailStats' -v`
Expected: FAIL — commands don't exist.

- [ ] **Step 5: Implement the two commands**

`cli/internal/cmd/email_events.go`:

```go
package cmd

import (
	"context"
	"encoding/json"
	"fmt"
	"text/tabwriter"

	"github.com/spf13/cobra"
)

func newEmailEventsCmd(opts *Options) *cobra.Command {
	return &cobra.Command{
		Use:   "events <email-id>",
		Short: "Show the delivery/engagement timeline for one email",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			return runEmailEvents(cmd.Context(), opts, args[0])
		},
	}
}

func runEmailEvents(ctx context.Context, opts *Options, emailID string) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	resp, err := apiClient.ListEmailEventsWithResponse(ctx, emailID)
	// ^ exact generated method name comes from the regenerated client;
	//   verify with: grep -n "emails/{email_id}/events" cli/internal/client/*.go
	if err != nil {
		return err
	}
	if resp.JSON200 == nil {
		return fmt.Errorf("api error: %s", resp.Status())
	}
	w := tabwriter.NewWriter(opts.stdout(), 0, 4, 2, ' ', 0)
	fmt.Fprintln(w, "KIND\tOCCURRED AT\tDETAIL")
	for _, e := range resp.JSON200.Items {
		detail, _ := json.Marshal(e.Payload)
		fmt.Fprintf(w, "%s\t%s\t%s\n", e.Kind, e.OccurredAt, string(detail))
	}
	return w.Flush()
}
```

`cli/internal/cmd/email_stats.go`:

```go
package cmd

import (
	"context"
	"fmt"
	"text/tabwriter"

	"github.com/spf13/cobra"
)

type emailStatsFlags struct {
	from   string
	to     string
	bucket string
}

func newEmailStatsCmd(opts *Options) *cobra.Command {
	f := &emailStatsFlags{}
	cmd := &cobra.Command{
		Use:   "stats",
		Short: "Account-level email deliverability stats",
		Args:  cobra.NoArgs,
		RunE: func(cmd *cobra.Command, _ []string) error {
			return runEmailStats(cmd.Context(), opts, f)
		},
	}
	cmd.Flags().StringVar(&f.from, "from", "", "Window start (RFC 3339; default now-7d)")
	cmd.Flags().StringVar(&f.to, "to", "", "Window end (RFC 3339; default now)")
	cmd.Flags().StringVar(&f.bucket, "bucket", "day", "Bucket size: hour|day")
	return cmd
}

func pct(v *float64) string {
	if v == nil {
		return "-"
	}
	return fmt.Sprintf("%.1f%%", *v*100)
}

func runEmailStats(ctx context.Context, opts *Options, f *emailStatsFlags) error {
	apiClient, err := opts.newClient()
	if err != nil {
		return err
	}
	// Build the generated params struct field-by-field; exact field names
	// come from the regenerated client (grep -n "emails/stats" cli/internal/client/*.go).
	params := &client.GetEmailStatsEmailsStatsGetParams{}
	if f.from != "" {
		params.From = &f.from
	}
	if f.to != "" {
		params.To = &f.to
	}
	if f.bucket != "" {
		b := client.GetEmailStatsEmailsStatsGetParamsBucket(f.bucket)
		params.Bucket = &b
	}
	resp, err := apiClient.GetEmailStatsWithResponse(ctx, params)
	// ^ verify generated names: grep -n "emails/stats" cli/internal/client/*.go
	if err != nil {
		return err
	}
	if resp.JSON200 == nil {
		return fmt.Errorf("api error: %s", resp.Status())
	}
	s := resp.JSON200
	w := tabwriter.NewWriter(opts.stdout(), 0, 4, 2, ' ', 0)
	fmt.Fprintf(w, "sent\t%d\n", s.Totals.Sent)
	fmt.Fprintf(w, "delivered\t%d\t%s\n", s.Totals.Delivered, pct(s.Rates.Delivery))
	fmt.Fprintf(w, "bounced (hard)\t%d (%d)\t%s\n", s.Totals.Bounced, s.Totals.BouncedHard, pct(s.Rates.Bounce))
	fmt.Fprintf(w, "complained\t%d\t%s\n", s.Totals.Complained, pct(s.Rates.Complaint))
	fmt.Fprintf(w, "opened (unique)\t%d (%d)\t%s\n", s.Totals.Opened, s.Totals.UniqueOpened, pct(s.Rates.Open))
	fmt.Fprintf(w, "clicked (unique)\t%d (%d)\t%s\n", s.Totals.Clicked, s.Totals.UniqueClicked, pct(s.Rates.Click))
	return w.Flush()
}
```

Register both in `cli/internal/cmd/email.go` next to the existing `AddCommand` calls:

```go
	cmd.AddCommand(newEmailEventsCmd(opts))
	cmd.AddCommand(newEmailStatsCmd(opts))
```

- [ ] **Step 6: Run tests**

Run: `cd cli && gofmt -l . && go test ./... `
Expected: no gofmt diffs; all tests PASS.

- [ ] **Step 7: Commit**

```bash
git add openapi/openapi.yaml cli/
git commit -m "feat(cli): hail email events + hail email stats"
```

---

### Task 14: MCP tools — `get_email_events`, `get_email_stats`

**Files:**

- Modify: `mcp/hailhq/mcp/tools.py`, the `HailClient` (find it: `grep -rn "class HailClient" mcp/`), MCP tool registration block (`*_tool` wrappers at the bottom of `tools.py`)
- Test: extend the MCP tools test file (`ls mcp/tests`)

**Interfaces:**

- Consumes: `GET /emails/{id}/events`, `GET /emails/stats`.
- Produces: MCP tools `get_email_events(email_id)` and `get_email_stats(from_=None, to=None, bucket="day")`, matching the one-tool-per-endpoint granularity.

- [ ] **Step 1: Write failing tests** — mirror the existing `list_emails` tool tests (they stub `HailClient` methods and assert passthrough/error shaping):

```python
async def test_get_email_events_passthrough(stub_client):
    stub_client.get_email_events.return_value = {"items": [{"kind": "delivered"}]}
    out = await tools.get_email_events(client=stub_client, email_id="abc")
    assert out["items"][0]["kind"] == "delivered"


async def test_get_email_stats_passthrough(stub_client):
    stub_client.get_email_stats.return_value = {"totals": {"sent": 2}}
    out = await tools.get_email_stats(client=stub_client)
    assert out["totals"]["sent"] == 2
```

(Adapt fixture names to the file's existing conventions.)

- [ ] **Step 2: Run to verify failure**

Run: `cd mcp && uv run pytest -k "email_events or email_stats" -v`
Expected: FAIL — functions don't exist.

- [ ] **Step 3: Implement**

In `HailClient`, add (mirroring `get_email` / `get_events` method style — httpx GETs against the API):

```python
    async def get_email_events(self, email_id: str) -> dict[str, Any]:
        return await self._get(f"/emails/{email_id}/events")

    async def get_email_stats(
        self,
        *,
        from_: str | None = None,
        to: str | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        params: dict[str, str] = {"bucket": bucket}
        if from_:
            params["from"] = from_
        if to:
            params["to"] = to
        return await self._get("/emails/stats", params=params)
```

(`_get` = whatever private request helper the existing methods use; match it.)

In `mcp/hailhq/mcp/tools.py`, after `get_email_attachment`:

```python
async def get_email_events(*, client: HailClient, email_id: str) -> dict[str, Any]:
    try:
        return await client.get_email_events(email_id)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_email_stats(
    *,
    client: HailClient,
    from_: str | None = None,
    to: str | None = None,
    bucket: str = "day",
) -> dict[str, Any]:
    try:
        return await client.get_email_stats(from_=from_, to=to, bucket=bucket)
    except ValidationError as exc:
        return {"error": _validation_error_message(exc)}
    except HailAPIError as exc:
        return _format_api_error(exc)
```

And register both in the tool-wiring block, mirroring `get_email_tool` (docstrings become the tool descriptions — one line on what the tool returns and when to use it):

```python
    @mcp.tool(name="get_email_events")
    async def get_email_events_tool(ctx: Context, email_id: str) -> dict[str, Any]:
        """Delivery/engagement timeline (sent→delivered→opened…) for one email."""
        async with _client_for(ctx) as client:
            return await get_email_events(client=client, email_id=email_id)

    @mcp.tool(name="get_email_stats")
    async def get_email_stats_tool(
        ctx: Context,
        from_: str | None = None,
        to: str | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        """Account-level email deliverability stats (counts, rates, time series)."""
        async with _client_for(ctx) as client:
            return await get_email_stats(client=client, from_=from_, to=to, bucket=bucket)
```

(Exact decorator/registration form: copy `get_email_tool`'s.)

- [ ] **Step 4: Run tests**

Run: `cd mcp && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcp/
git commit -m "feat(mcp): get_email_events + get_email_stats tools"
```

---

### Task 15: Python SDK — `emails.events()` + `emails.stats()`

**Files:**

- Modify: `sdk/hail/client.py`
- Test: extend the SDK's emails test file (`ls sdk/tests` and follow its mock-transport pattern)

**Interfaces:**

- Consumes: `GET /emails/{id}/events`, `GET /emails/stats`.
- Produces: on the Emails resource class in `sdk/hail/client.py` (the one with `create`/`get`/`list` at ~line 131): `async def events(self, email_id) -> dict[str, Any]` and `async def stats(self, *, from_=None, to=None, bucket="day") -> dict[str, Any]`. Plain-dict returns, matching the webhooks resource precedent (no new model classes — YAGNI).

- [ ] **Step 1: Write the failing test** — mirror the existing SDK emails tests (mocked HTTP layer asserting path + params):

```python
async def test_email_events_path(sdk_client, mock_http):
    mock_http.expect_get("/emails/abc/events", {"items": []})
    out = await sdk_client.emails.events("abc")
    assert out == {"items": []}


async def test_email_stats_params(sdk_client, mock_http):
    mock_http.expect_get(
        "/emails/stats",
        {"totals": {"sent": 0}},
        params={"bucket": "day", "from": "2026-06-01T00:00:00Z"},
    )
    out = await sdk_client.emails.stats(from_="2026-06-01T00:00:00Z")
    assert out["totals"]["sent"] == 0
```

(Adapt helper names to the actual test harness in `sdk/tests` — assert on request path, query params, and passthrough of the JSON body.)

- [ ] **Step 2: Run to verify failure**

Run: `cd sdk && uv run pytest -k "events_path or stats_params" -v`
Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement** — in the Emails resource class in `sdk/hail/client.py`, after `list`:

```python
    async def events(self, email_id: str | UUID) -> dict[str, Any]:
        """Delivery/engagement timeline for one email."""
        return await self._http.get(f"/emails/{email_id}/events")

    async def stats(
        self,
        *,
        from_: str | datetime | None = None,
        to: str | datetime | None = None,
        bucket: str = "day",
    ) -> dict[str, Any]:
        """Account-level deliverability stats for a time window."""
        params: dict[str, Any] = {"bucket": bucket}
        if from_ is not None:
            params["from"] = from_.isoformat() if isinstance(from_, datetime) else from_
        if to is not None:
            params["to"] = to.isoformat() if isinstance(to, datetime) else to
        return await self._http.get("/emails/stats", params=params)
```

(`self._http.get` = whatever request helper the class's existing `get`/`list` methods use — match their call shape exactly, including any response-unwrapping.)

- [ ] **Step 4: Run tests**

Run: `cd sdk && uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add sdk/
git commit -m "feat(sdk): emails.events() + emails.stats()"
```

---

### Task 16: Docs + changelog

**Files:**

- Modify: `docs/setup/aws-ses.md`, `docs/setup/webhooks.md`, `CHANGELOG.md`

**Interfaces:** none (prose).

- [ ] **Step 1: `docs/setup/aws-ses.md`** — add a "Delivery & engagement events" section after the inbound section, agent-first style (runnable example leading):

````markdown
## Delivery & engagement events

Outbound sends carry the SES configuration set named by
`HAIL_SES_CONFIGURATION_SET` (Terraform default: `hail-events`). SES
publishes Delivery / Bounce / Complaint / Reject / DeliveryDelay / Open /
Click events to SNS; the ingest Lambda relays them to
`POST /internal/ses-events`, which records them in `email_events`,
advances `emails.status`, and fans out webhooks.

Check a single email's timeline:

```bash
hail email events <email-id>
```

Account-level stats:

```bash
hail email stats --from 2026-06-01T00:00:00Z --bucket day
```

Notes:

- Open/Click tracking rewrites links through the default SES tracking
  domain. A custom tracking domain is not yet supported.
- Open counts are approximate (image-proxying mail clients inflate them).
- Events for mail sent outside Hail from the same SES account are
  acknowledged and dropped (`status: unmatched` in the API log).
````

- [ ] **Step 2: `docs/setup/webhooks.md`** — extend the event-type table with `email.delivered`, `email.delivery_delayed`, `email.bounced`, `email.complained`, `email.opened`, `email.clicked` and one example payload:

```json
{
  "organization_id": "…",
  "data": {
    "id": "<email uuid>",
    "kind": "bounced",
    "occurred_at": "2026-07-01T12:00:05+00:00",
    "from_address": "noreply@acme.com",
    "to_addresses": ["bob@example.com"],
    "subject": "Welcome",
    "detail": {
      "bounce_type": "Permanent",
      "bounce_sub_type": "General",
      "recipients": ["bob@example.com"],
      "diagnostic_code": "smtp; 550 5.1.1 user unknown"
    }
  }
}
```

Remove the "only emitted once SES bounce/complaint ingestion lands" caveat.

- [ ] **Step 3: `CHANGELOG.md`** — add under Unreleased:

```markdown
- Email deliverability tracking: `email_events` table, SES configuration-set
  event ingestion (delivered/bounced/complained/rejected/delayed/opened/clicked),
  `GET /emails/{id}/events`, `GET /emails/stats`, lifecycle webhook events,
  email events on `GET /events`, `hail email events|stats`, MCP
  `get_email_events`/`get_email_stats`.
```

- [ ] **Step 4: Commit**

```bash
git add docs/setup/aws-ses.md docs/setup/webhooks.md CHANGELOG.md
git commit -m "docs: email deliverability events setup + webhook payloads"
```

---

## Final verification (after all tasks)

- [ ] `cd api && uv run pytest -q` — all green
- [ ] `cd core && uv run pytest -q` — all green
- [ ] `cd mcp && uv run pytest -q` — all green
- [ ] `cd cli && go test ./...` — all green
- [ ] `cd infra/terraform && terraform validate` — valid
- [ ] `git log --oneline` shows one conventional commit per task
- [ ] `openapi/openapi.yaml` contains `/emails/stats`, `/emails/{email_id}/events`, and the widened `WebhookEventType` + `EmailStatus` enums
