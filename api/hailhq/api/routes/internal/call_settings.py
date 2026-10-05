"""Effective call limits for the authenticated console backend."""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends
from hailhq.api.routes.internal.auth import verify_internal_request
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.disclosure import DEFAULT_INBOUND_LINE, DEFAULT_OUTBOUND_LINE
from hailhq.core.models import OrganizationCallSettings
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(
    prefix="/internal",
    include_in_schema=False,
    dependencies=[Depends(verify_internal_request)],
)


@router.get("/orgs/{organization_id}/call-settings")
async def get_call_settings(
    organization_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
) -> dict:
    override = await db.get(OrganizationCallSettings, organization_id)
    default = settings.hail_voice_max_duration_seconds
    seconds = override.max_duration_seconds if override else None
    return {
        "max_duration_seconds": seconds or default,
        "default_max_duration_seconds": default,
        "uses_default": seconds is None,
        # Workspace AI line template ('{org}' = the organization name); null
        # means the built-in line for the call's direction.
        "ai_disclosure_line": override.ai_disclosure_line if override else None,
        "default_ai_disclosure_lines": {
            "outbound": DEFAULT_OUTBOUND_LINE,
            "inbound": DEFAULT_INBOUND_LINE,
        },
    }
