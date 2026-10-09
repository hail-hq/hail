"""Apply one SES delivery event: dedup insert, status transition, fanout.

Transaction discipline: this function flushes but never commits — the
caller (the /internal/ses-events route) owns the transaction so the event
row, the status change, and the webhook delivery rows land atomically.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, get_args
from uuid import UUID, uuid4

from hailhq.core.config import settings
from hailhq.core.forward_targets import (
    normalize_address,
    stop_targets,
    verified_member_emails,
)
from hailhq.core.models import Email, EmailDomain, EmailEvent
from hailhq.core.providers.email.inbound.ses_delivery import DeliveryEvent
from hailhq.core.schemas import EmailEventKind
from hailhq.core.system_email import (
    SYSTEM_KIND_FORWARD_STOPPED,
    enqueue_system_email,
    forwarder_address,
    render_forward_stopped,
)
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer

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

    if event.kind == "complained" and (email.metadata_ or {}).get("forwarded_from"):
        # First delivery of this complaint only (the dedup insert above
        # returned early on a redelivery), so a target is stopped and the
        # owners notified exactly once per complaint.
        await _stop_forwarding_on_complaint(db, email, event)

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


async def _stop_forwarding_on_complaint(
    db: AsyncSession, email: Email, event: DeliveryEvent
) -> None:
    """A spam complaint on a forward stops that target and tells the owners.

    Only providers with a feedback loop to SES (Yahoo, Microsoft, ...) send
    complaints here; Gmail does not. The notice goes to verified owner /
    admin logins except the complained address itself — SES's account
    suppression blocks that one anyway. When nobody is left, the console
    badge is the only signal.
    """
    to_addresses = [normalize_address(a) for a in (email.to_addresses or [])]
    reported = [
        normalize_address(r)
        for r in (event.detail.get("recipients") or [])
        if normalize_address(r) in to_addresses
    ]
    addresses = reported or to_addresses
    if not addresses:
        return
    stopped = await stop_targets(
        db,
        email.organization_id,
        addresses,
        reason="complaint",
        email_id=email.id,
    )
    if not stopped or email.email_domain_id is None:
        return

    domain = await db.get(EmailDomain, email.email_domain_id)
    sender = forwarder_address(
        email.organization_id,
        domain.local_prefix_org if domain is not None else None,
        settings.hail_mail_base_domain,
    )
    owners = await verified_member_emails(
        db, email.organization_id, roles=("owner", "admin")
    )
    stopped_addresses = {t.address for t in stopped}
    for target in stopped:
        subject, text, html = render_forward_stopped(address=target.address)
        for owner in sorted(owners - stopped_addresses):
            await enqueue_system_email(
                db,
                organization_id=email.organization_id,
                email_domain_id=email.email_domain_id,
                from_address=sender,
                to=owner,
                subject=subject,
                body_text=text,
                body_html=html,
                kind=SYSTEM_KIND_FORWARD_STOPPED,
            )
