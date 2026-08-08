"""POST /internal/numbers/release — hail-website → API.

Called by hail-website's dunning job (app/api/internal/billing/dunning)
when an org has held active dedicated numbers on a zero or negative
balance past the announced grace deadline. Releases one number at the
carrier and marks the row released, via the same helpers as the public
DELETE /numbers/{id} so the two paths cannot drift.

Shared-secret HMAC auth, same as the rest of ``routes/internal/`` — see
``routes/internal/auth.py``. Not in the public OpenAPI spec.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from hailhq.api.audit import write_audit_log
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.api.routes.numbers import (
    _get_org_number_or_404,
    get_voice_provider,
    release_org_number,
)
from hailhq.core.db import get_session
from hailhq.core.providers.voice import VoiceProvider
from pydantic import BaseModel, Field
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
    # Free-form provenance, e.g. "dunning". Bounded so a crafted body can't
    # flood the log line / audit payload it lands in.
    source: str = Field(default="hail_website", max_length=64)


@router.post("/numbers/release")
async def release_number_internal(
    body: NumberReleaseIn,
    db: Annotated[AsyncSession, Depends(get_session)],
    provider: Annotated[VoiceProvider, Depends(get_voice_provider)],
) -> dict:
    # Same lookup as the public routes (no is_pool filter needed: the
    # phone_numbers_pool_owner_xor CHECK guarantees a row matching a non-null
    # organization_id is never a pool row).
    number = await _get_org_number_or_404(db, body.number_id, body.organization_id)
    # `source` is provenance-only; log it so a dunning release and a future
    # operator release are distinguishable after the fact (`released_at` says
    # when, this says why).
    logger.info(
        "internal release of number %s (org %s, source=%s)",
        body.number_id,
        body.organization_id,
        body.source,
    )
    was_released = number.provisioning_state == "released"
    number = await release_org_number(db, provider, number)
    if not was_released:
        # Same audit action as DELETE /numbers/{id}; api_key_id is None
        # because no API key acts here — `source` says who did.
        await write_audit_log(
            organization_id=body.organization_id,
            api_key_id=None,
            action="number.release",
            resource_type="phone_number",
            resource_id=number.id,
            payload={"e164": number.e164, "source": body.source},
        )
    return {
        "number_id": str(number.id),
        "e164": number.e164,
        "provisioning_state": number.provisioning_state,
        "released_at": number.released_at.isoformat() if number.released_at else None,
    }
