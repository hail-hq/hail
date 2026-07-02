"""Apply one SES delivery event: dedup insert, status transition, fanout.

Transaction discipline: this function flushes but never commits — the
caller (the /internal/ses-events route) owns the transaction so the event
row, the status change, and the webhook delivery rows land atomically.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, get_args
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

from hailhq.core.models import Email, EmailEvent
from hailhq.core.providers.email.inbound.ses_delivery import DeliveryEvent
from hailhq.core.schemas import EmailEventKind

__all__ = [
    "ApplyResult",
    "apply_delivery_event",
    "build_delivery_event_data",
    "record_sent_event",
]

# kind → statuses it may transition FROM (guarded UPDATE … WHERE status IN).
_STATUS_FROM: dict[str, tuple[str, ...]] = {
    "delivered": ("sent",),
    "bounced": ("sent", "delivered"),  # hard bounces only (checked below)
    "complained": ("sent", "delivered", "bounced"),
    "rejected": ("queued", "sent"),
}

# Kinds that fan out to customer webhooks as ``email.<kind>``. ``sent`` is
# synthetic (written by us at send time, not subscribable) and ``rejected``
# is a send failure (surfaces as status=failed), so both are excluded.
_FANOUT_KINDS = frozenset(get_args(EmailEventKind)) - {"sent", "rejected"}

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


def record_sent_event(
    session: AsyncSession,
    *,
    email_id: UUID,
    organization_id: UUID,
    occurred_at: datetime,
) -> None:
    """Add the synthetic ``sent`` event row written at send time.

    SES has no consumable Send event (the parser deliberately skips it), so
    every send path — direct POST /emails and the forward worker — records
    this row itself, through here so the shape can't drift between them.
    """
    session.add(
        EmailEvent(
            email_id=email_id,
            organization_id=organization_id,
            kind="sent",
            payload={},
            occurred_at=occurred_at,
        )
    )


def _new_status_for(email_status: str, event: DeliveryEvent) -> str | None:
    if event.kind == "bounced" and not event.detail.get("hard"):
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
            select(Email)
            # Hot path (one fetch per webhook, opens/clicks fire repeatedly):
            # skip the unbounded body columns; nothing here reads them.
            .options(defer(Email.body_text), defer(Email.body_html)).where(
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
