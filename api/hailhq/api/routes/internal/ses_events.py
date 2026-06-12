"""POST /internal/ses-events — ses-ingest-lambda → API.

HMAC-signed by the Lambda over the raw body (``X-Hail-Signature`` header).
On a valid notification:
  - parse the small JSON envelope into an ``InboundMessage``
  - hand it to the ingest service, which fetches raw MIME from S3, parses,
    routes by hail-mail local-part, persists one ``Email`` row per matched
    org plus attachments, and short-circuits duplicates by message_id

then enqueues forwarding sends and webhook fan-out for the persisted rows.

Not in the public OpenAPI spec: this is operator infrastructure → API.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi import status as http_status
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.outbound_queue import enqueue_outbound_forward
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.email_ingest import ingest_inbound
from hailhq.core.providers.email.inbound.ses import SesInboundProvider
from hailhq.core.s3_inbound import S3InboundClient
from hailhq.core.urls import canonical_url
from hailhq.core.webhook_fanout import fanout_email_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/internal", tags=["internal"], include_in_schema=False)


def require_inbound_enabled() -> None:
    """Run before provider construction so the 503 wins over config errors."""
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


def get_s3_inbound_client() -> S3InboundClient:
    return S3InboundClient(bucket=settings.hail_inbound_bucket)


@router.post("/ses-events", dependencies=[Depends(require_inbound_enabled)])
async def receive_ses_event(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[SesInboundProvider, Depends(get_inbound_provider)],
    s3: Annotated[S3InboundClient, Depends(get_s3_inbound_client)],
    x_hail_signature: Annotated[str | None, Header()] = None,
) -> dict[str, list[str]]:
    body = await request.body()
    headers = {"X-Hail-Signature": x_hail_signature or ""}
    if not await provider.verify_notification(headers, body):
        raise HTTPException(
            status_code=http_status.HTTP_401_UNAUTHORIZED,
            detail="invalid signature",
        )

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
    )
    return {
        "email_ids": [str(x) for x in result.email_ids],
        "skipped_recipients": result.skipped_recipients,
        "suppressed_reasons": result.suppressed_reasons,
    }
