"""Outbound enqueue helper used by ingest-driven forwarding.

The ingest service invokes this once per forward target. It writes an
``Email`` row with ``status='queued'`` and ``metadata.forwarded_from``
pointing at the inbound id; the ``OutboundForwardWorker`` (started in
main.py's lifespan when inbound is enabled) claims the row and makes
the SES call.

This is the bridge between the pure-core ingest pipeline and the API's
outbound side — kept in api/ so core/ doesn't need to know about email
queue mechanics.
"""

from __future__ import annotations

from uuid import UUID

from hailhq.core.models import Email
from sqlalchemy.ext.asyncio import AsyncSession


async def enqueue_outbound_forward(
    db: AsyncSession,
    *,
    organization_id: UUID,
    email_domain_id: UUID,
    from_address: str,
    to: str,
    reply_to: str | None,
    subject: str,
    body_text: str | None,
    body_html: str | None,
    headers: dict[str, str],
    inbound_id: UUID,
) -> None:
    email = Email(
        organization_id=organization_id,
        email_domain_id=email_domain_id,
        from_address=from_address,
        to_addresses=[to],
        reply_to=reply_to,
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        status="queued",
        provider="ses",
        direction="outbound",
        metadata_={
            "forwarded_from": str(inbound_id),
            "forward_headers": headers,
        },
    )
    db.add(email)
    await db.flush()
