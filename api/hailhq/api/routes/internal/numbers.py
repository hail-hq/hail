"""POST /internal/numbers/release — hail-website → API.

Called by hail-website's dunning job (app/api/internal/billing/dunning)
when an org has held active dedicated numbers on a zero or negative
balance past the announced grace deadline. Releases one number at the
carrier and marks the row released, via the same helper as the public
DELETE /numbers/{id} so the two paths cannot drift.

Shared-secret HMAC auth, same as the rest of ``routes/internal/`` — see
``routes/internal/auth.py``. Not in the public OpenAPI spec.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from fastapi import status as http_status
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.numbers import get_voice_provider, release_org_number
from hailhq.core.db import get_session
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.voice import VoiceProvider
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/internal",
    tags=["internal"],
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


class NumberReleaseIn(BaseModel):
    organization_id: UUID
    number_id: UUID
    # Free-form provenance, e.g. "dunning".
    source: str = "hail_website"


@router.post("/numbers/release")
async def release_number_internal(
    body: NumberReleaseIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
) -> dict:
    number = (
        await db.execute(
            select(PhoneNumber).where(
                PhoneNumber.id == body.number_id,
                PhoneNumber.organization_id == body.organization_id,
                PhoneNumber.is_pool.is_(False),
            )
        )
    ).scalar_one_or_none()
    if number is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="number not found"
        )
    # `source` is provenance-only; log it so a dunning release and a future
    # operator release are distinguishable after the fact (`released_at` says
    # when, this says why).
    logger.info(
        "internal release of number %s (org %s, source=%s)",
        body.number_id,
        body.organization_id,
        body.source,
    )
    number = await release_org_number(db, provider, number)
    return {
        "number_id": str(number.id),
        "e164": number.e164,
        "provisioning_state": number.provisioning_state,
        "released_at": number.released_at.isoformat() if number.released_at else None,
    }
