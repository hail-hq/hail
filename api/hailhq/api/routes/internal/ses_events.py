"""POST /internal/ses-events — ses-ingest-lambda → API.

HMAC-signed by the Lambda over the raw body (``X-Hail-Signature`` header).
On a valid notification:
  - parse the small JSON envelope into an ``InboundMessage``
  - hand it to the ingest service, which fetches raw MIME from S3, parses,
    routes by hail-mail local-part, persists one ``Email`` row per matched
    org plus attachments, and short-circuits duplicates by message_id

then enqueues forwarding sends and webhook fan-out for the persisted rows.

A second envelope shape, ``{"type": "delivery_event", "event": {...}}``,
carries SES configuration-set delivery/engagement events (Delivery, Bounce,
Complaint, ...) and is handled independently of ``HAIL_INBOUND_ENABLED`` —
see ``_handle_delivery_event`` below.

Not in the public OpenAPI spec: this is operator infrastructure → API.
"""

from __future__ import annotations

import json
import logging
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.errors import unprocessable
from hailhq.api.outbound_queue import enqueue_outbound_forward
from hailhq.api.usage import write_usage_event
from hailhq.core.billing import has_funds
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.email_delivery_events import apply_delivery_event
from hailhq.core.email_ingest import ingest_inbound
from hailhq.core.providers.email.inbound.ses import SesInboundProvider
from hailhq.core.providers.email.inbound.ses_delivery import parse_delivery_event
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.urls import canonical_url
from hailhq.core.webhook_fanout import fanout_email_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def require_inbound_enabled() -> None:
    """Gate legacy inbound envelopes on ``HAIL_INBOUND_ENABLED``.

    Called explicitly by the handler, before signature verification, for any
    envelope that isn't a ``delivery_event`` — so a disabled legacy inbound
    pipeline 503s regardless of signature validity, matching pre-restructure
    behavior. ``delivery_event`` envelopes never call this and work with
    inbound disabled.

    The ``get_inbound_provider`` dependency (HMAC secret missing → 503) still
    resolves before the handler body runs at all, so that check wins over
    everything, including this one and signature verification, for both
    envelope shapes.
    """
    if not settings.hail_inbound_enabled:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound disabled",
        )


def get_inbound_provider() -> SesInboundProvider:
    try:
        return SesInboundProvider(hmac_secret=settings.hail_inbound_hmac_secret)
    except ValueError as exc:
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="inbound enabled but HAIL_INBOUND_HMAC_SECRET is unset",
        ) from exc


def get_s3_mail_client() -> S3MailClient:
    if not settings.hail_inbound_enabled:
        # Never used: the delivery_event branch skips S3 entirely and the
        # legacy inbound branch 503s via require_inbound_enabled() first.
        # Constructing the real client here would demand a bucket even for
        # delivery-only deployments running with inbound disabled.
        return cast(S3MailClient, None)
    return S3MailClient(bucket=settings.hail_mail_bucket)


@router.post("/ses-events")
async def receive_ses_event(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SesInboundProvider, Depends(get_inbound_provider)],
    s3: Annotated[S3MailClient, Depends(get_s3_mail_client)],
    x_hail_signature: Annotated[str | None, Header()] = None,
) -> dict:
    body = await request.body()
    headers = {"X-Hail-Signature": x_hail_signature or ""}

    # Lenient sniff, purely to route: is this a delivery_event? Any parse
    # failure here means "not a delivery_event" — the legacy branch below
    # does its own strict (400-raising) parse after the 503/401 gates.
    try:
        envelope = json.loads(body)
        is_delivery = (
            isinstance(envelope, dict) and envelope.get("type") == "delivery_event"
        )
    except ValueError:
        envelope = None
        is_delivery = False

    if not is_delivery:
        # Legacy inbound envelope (or unparseable body) — 503 wins over
        # signature verification and parse errors, matching pre-restructure
        # behavior byte-for-byte.
        require_inbound_enabled()

    if not await provider.verify_notification(headers, body):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )

    if is_delivery:
        return await _handle_delivery_event(db, envelope)

    try:
        message = await provider.parse_notification(body)
    except Exception as exc:
        raise HTTPException(
            status_code=http_status.HTTP_400_BAD_REQUEST,
            detail="malformed SES notification payload",
        ) from exc
    result = await ingest_inbound(
        db,
        message=message,
        s3=s3,
        hail_mail_base_domain=settings.hail_mail_base_domain,
        forward_enqueue=enqueue_outbound_forward,
        forward_max_hops=settings.hail_forward_max_hops,
        forward_default_per_hour=settings.hail_forward_rate_per_hour,
        fanout=fanout_email_event,
        api_base_url=canonical_url(str(request.base_url)),
        org_rate_per_hour=settings.hail_inbound_org_rate_per_hour,
        funds_check=has_funds,
    )
    for created_id, created_org_id in result.created_email_ids:
        await write_usage_event(
            organization_id=created_org_id,
            channel="email",
            units=1,
            ref=f"email:{created_id}",
        )
    return {
        "email_ids": [str(x) for x in result.email_ids],
        "skipped_recipients": result.skipped_recipients,
        "suppressed_reasons": result.suppressed_reasons,
    }


async def _handle_delivery_event(db: AsyncSession, envelope: dict) -> dict:
    """Handle the ``{"type": "delivery_event", "event": {...}}`` branch.

    Does not require ``hail_inbound_enabled`` — only the HMAC secret
    (verified by the caller before we get here).
    """
    raw_event = envelope.get("event")
    if not isinstance(raw_event, dict):
        raise unprocessable(
            "delivery_event envelope missing 'event' object",
            loc=["body", "event"],
        )
    try:
        event = parse_delivery_event(raw_event)
    except (KeyError, ValueError, TypeError) as exc:
        raise unprocessable(
            "malformed SES delivery event", loc=["body", "event"]
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
