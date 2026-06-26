"""Fan-out service: enqueue webhook_deliveries rows for one email event.

The delivery worker does the POSTing; this service only writes the rows.
One row is created per active org-wide ``WebhookSubscription`` whose
``event_types`` includes the fired event. Each row is stamped with the
source ``email_domain_id`` (when known) so the worker can emit the
informational ``X-Hail-Email-Domain`` header; routing is purely by
subscription.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import WebhookDelivery, WebhookSubscription

__all__ = ["build_event_data", "fanout_email_event"]


def build_event_data(
    *,
    email_id: str,
    direction: str,
    from_address: str,
    to_addresses: list[str],
    subject: str,
    message_id: str | None,
    in_reply_to: str | None,
    spam_verdict: str | None,
    virus_verdict: str | None,
    spf_verdict: str | None,
    dkim_verdict: str | None,
    dmarc_verdict: str | None,
    raw_url: str | None,
    attachments: list[dict[str, Any]],
    email_domain: str | None = None,
) -> dict[str, Any]:
    return {
        "id": email_id,
        "direction": direction,
        "email_domain": email_domain,
        "from_address": from_address,
        "to_addresses": to_addresses,
        "subject": subject,
        "message_id": message_id,
        "in_reply_to": in_reply_to,
        "spam_verdict": spam_verdict,
        "virus_verdict": virus_verdict,
        "spf_verdict": spf_verdict,
        "dkim_verdict": dkim_verdict,
        "dmarc_verdict": dmarc_verdict,
        "raw_url": raw_url,
        "attachments": attachments,
    }


async def fanout_email_event(
    db: AsyncSession,
    *,
    organization_id: UUID,
    email_domain_id: UUID | None,
    event_type: str,
    event_id: UUID,
    data: dict[str, Any],
) -> int:
    """Insert delivery rows. Returns the number inserted."""
    inserted = 0

    subs = (
        (
            await db.execute(
                select(WebhookSubscription).where(
                    WebhookSubscription.organization_id == organization_id,
                    WebhookSubscription.status == "active",
                )
            )
        )
        .scalars()
        .all()
    )
    for sub in subs:
        if event_type not in (sub.event_types or []):
            continue
        db.add(
            WebhookDelivery(
                subscription_id=sub.id,
                email_domain_id=email_domain_id,
                event_type=event_type,
                event_id=event_id,
                payload=_payload(organization_id, data),
            )
        )
        inserted += 1

    if inserted:
        await db.flush()
    return inserted


def _payload(organization_id: UUID, data: dict[str, Any]) -> dict[str, Any]:
    # The worker assembles the §5.2 envelope (id, type, api_version,
    # created_at) at send time via build_event_payload. We persist only
    # the org + data it needs. event_type lives on the delivery row.
    return {
        "organization_id": str(organization_id),
        "data": data,
    }
