"""Org-scoped playback and authenticated LiveKit recording completion."""

from __future__ import annotations

import asyncio
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from hailhq.api.deps import get_current_principal
from hailhq.api.ratelimit import exempt_path
from hailhq.api.routes.calls import get_livekit
from hailhq.core.db import get_session
from hailhq.core.livekit import verify_webhook_event
from hailhq.core.models import Call
from hailhq.core.recordings import apply_egress_info, presign_recording
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

router = APIRouter(tags=["calls"])
# Signature-authenticated by LiveKit, no Bearer token, so it must not share
# the anonymous remote-IP bucket.
exempt_path("/livekit/recording-events")


async def reconcile_recording(db: AsyncSession, call: Call) -> None:
    meta = call.metadata_ or {}
    egress_id = meta.get("recording_egress_id")
    if not egress_id or meta.get("recording_status") in {
        "ready",
        "failed",
        "unavailable",
        "erased",
    }:
        return
    try:
        livekit = await get_livekit()
        infos = await asyncio.wait_for(livekit.list_egress(egress_id), timeout=3)
        for info in infos:
            await apply_egress_info(db, call, info)
        await db.commit()
        await db.refresh(call)
    except Exception:
        await db.rollback()
        await db.refresh(call)


@router.get(
    "/calls/{call_id}/recording", response_class=RedirectResponse, status_code=302
)
async def get_recording(
    call_id: UUID,
    db: Annotated[AsyncSession, Depends(get_session)],
    principal=Depends(get_current_principal),
):
    """Redirect to a short-lived recording URL. Returns 404 if unavailable or expired."""
    call = (
        await db.execute(
            select(Call).where(
                Call.id == call_id, Call.organization_id == principal.organization_id
            )
        )
    ).scalar_one_or_none()
    if call is None:
        raise HTTPException(404, "Recording not found")
    await reconcile_recording(db, call)
    url = await presign_recording(call)
    if url is None:
        raise HTTPException(404, "Recording unavailable or expired")
    return RedirectResponse(
        url, status_code=302, headers={"Cache-Control": "private, no-store"}
    )


@router.post("/livekit/recording-events", include_in_schema=False)
async def recording_event(
    request: Request, db: Annotated[AsyncSession, Depends(get_session)]
):
    body = await request.body()
    if len(body) > 262144:
        raise HTTPException(413, "Event too large")
    try:
        event = verify_webhook_event(
            body.decode(), request.headers.get("authorization", "")
        )
    except Exception:
        raise HTTPException(401, "Invalid LiveKit event") from None
    if event.event not in {"egress_started", "egress_updated", "egress_ended"}:
        return {"ok": True}
    info = event.egress_info
    call = (
        await db.execute(
            select(Call)
            .where(
                Call.livekit_room == info.room_name,
                Call.metadata_["recording_egress_id"].astext == info.egress_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if call:
        await apply_egress_info(db, call, info)
        await db.commit()
    return {"ok": True}
