"""Fan-out service: enqueue webhook_deliveries rows for one email event.

The delivery worker (Phase 6) does the POSTing; this service only writes
the rows. A single inbound event can produce two deliveries:

* The per-domain row, when ``email_domains.webhook_url`` is configured
  and ``inbound_enabled`` is True.
* One row per active org-wide ``WebhookSubscription`` whose
  ``event_types`` includes the fired event.

The two paths fire independently — if both are set, the tenant gets
both POSTs. Duplicates by design (explicit beats clever).
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailDomain, WebhookDelivery, WebhookSubscription

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
) -> dict[str, Any]:
    return {
        "id": email_id,
        "direction": direction,
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

    # Per-domain webhook target (Cloudflare Email Routing-style ergonomic).
    if email_domain_id is not None:
        dom = (
            await db.execute(
                select(EmailDomain).where(EmailDomain.id == email_domain_id)
            )
        ).scalar_one_or_none()
        if dom is not None and dom.webhook_url and dom.inbound_enabled:
            db.add(
                WebhookDelivery(
                    subscription_id=None,
                    email_domain_id=dom.id,
                    event_type=event_type,
                    event_id=event_id,
                    payload=_payload(organization_id, data),
                )
            )
            inserted += 1

    # Org-wide multi-event subscriptions.
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
                email_domain_id=None,
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
