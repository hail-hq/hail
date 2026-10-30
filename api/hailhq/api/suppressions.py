"""Shared bodies for the per-channel suppression routes.

Used by ``GET/DELETE /v1/sms/suppressions`` and
``GET/DELETE /v1/emails/suppressions``. Both list only the org's own rows
for one channel and delete through ``remove_suppression``, which never
touches platform-wide rows (``organization_id IS NULL``).
"""

from __future__ import annotations

from uuid import UUID

from fastapi import HTTPException
from fastapi import status as http_status
from hailhq.api.pagination import fetch_cursor_page
from hailhq.core.compliance_gate import remove_suppression
from hailhq.core.models import Suppression
from hailhq.core.schemas import SuppressionListResponse, SuppressionResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def list_channel_suppressions(
    db: AsyncSession,
    *,
    organization_id: UUID,
    channel: str,
    cursor: str | None,
    limit: int,
) -> SuppressionListResponse:
    """One page of the org's suppressions on ``channel``, newest first."""
    stmt = select(Suppression).where(
        Suppression.organization_id == organization_id,
        Suppression.channel == channel,
    )
    rows, next_cursor = await fetch_cursor_page(
        db,
        stmt,
        Suppression.created_at,
        Suppression.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return SuppressionListResponse(
        items=[SuppressionResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


async def delete_channel_suppression(
    db: AsyncSession, *, organization_id: UUID, recipient: str, channel: str
) -> None:
    """Delete the org's rows for ``recipient`` on ``channel`` and commit.

    Raises 404 when the org has no such row.
    """
    removed = await remove_suppression(
        db, organization_id=organization_id, recipient=recipient, channel=channel
    )
    if not removed:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail="suppression not found"
        )
    await db.commit()
