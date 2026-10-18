"""Best-effort LiveKit audio-only recording. Failures never own the call."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.models import Call
from hailhq.core.recordings import (
    apply_egress_info,
    recording_retention_days,
    recording_state,
)
from livekit import api

logger = logging.getLogger(__name__)


async def start_recording(call_id: UUID, room_name: str, lkapi) -> None:
    """One Egress per call. Never retry a full quota or ambiguous start."""
    try:
        async with session_scope() as db:
            call = await db.get(Call, call_id, with_for_update=True)
            if call is None or not (call.metadata_ or {}).get("recording_enabled"):
                return
            if (call.metadata_ or {}).get("recording_status") != "requested":
                return
            if not settings.hail_recording_enabled or not settings.s3_bucket:
                await recording_state(db, call_id, recording_status="unavailable")
                await db.commit()
                return
            days = recording_retention_days(call)
            key = f"recordings/{days}d/{call.organization_id}/{call_id}/audio.ogg"
            await recording_state(
                db, call_id, recording_status="starting", recording_object_key=key
            )
            await db.commit()
        request = api.RoomCompositeEgressRequest(
            room_name=room_name,
            audio_only=True,
            audio_mixing=api.DUAL_CHANNEL_AGENT,
            advanced=api.EncodingOptions(
                audio_codec=api.OPUS, audio_bitrate=128, audio_frequency=48000
            ),
            file_outputs=[
                api.EncodedFileOutput(
                    file_type=api.OGG,
                    filepath=key,
                    # No sidecar manifest: erasure deletes exactly one object.
                    disable_manifest=True,
                    s3=api.S3Upload(
                        bucket=settings.s3_bucket,
                        region=settings.s3_region,
                        endpoint=settings.s3_endpoint,
                        access_key=settings.s3_access_key,
                        secret=settings.s3_secret_key,
                        force_path_style=bool(settings.s3_endpoint),
                    ),
                )
            ],
        )
        info = await asyncio.wait_for(
            lkapi.egress.start_room_composite_egress(request), timeout=5
        )
        async with session_scope() as db:
            await recording_state(db, call_id, recording_egress_id=info.egress_id)
            call = await db.get(Call, call_id, populate_existing=True)
            if call:
                await apply_egress_info(db, call, info)
            await db.commit()
    except Exception:
        logger.warning(
            "recording unavailable for call_id=%s; continuing call",
            call_id,
            exc_info=True,
        )
        try:
            async with session_scope() as db:
                await recording_state(db, call_id, recording_status="unavailable")
                await db.commit()
        except Exception:
            logger.warning(
                "recording state write failed for call_id=%s", call_id, exc_info=True
            )


async def _active_egress_id(room_name: str | None, lkapi) -> str | None:
    """An Egress LiveKit started although our start task was cancelled first."""
    if not room_name:
        return None
    result = await asyncio.wait_for(
        lkapi.egress.list_egress(
            api.ListEgressRequest(room_name=room_name, active=True)
        ),
        timeout=3,
    )
    for info in result.items:
        return info.egress_id
    return None


async def stop_recording(call_id: UUID, lkapi, room_name: str | None = None) -> None:
    try:
        async with session_scope() as db:
            call = await db.get(Call, call_id)
            meta = (call.metadata_ or {}) if call else {}
            egress_id = meta.get("recording_egress_id")
            if not egress_id and meta.get("recording_status") == "starting":
                # The start was cancelled before LiveKit answered. LiveKit may
                # still have started the Egress, so look it up by room before
                # giving up; never leave the row "starting".
                egress_id = await _active_egress_id(room_name, lkapi)
                if egress_id:
                    await recording_state(db, call_id, recording_egress_id=egress_id)
                else:
                    await recording_state(db, call_id, recording_status="unavailable")
                await db.commit()
        if not egress_id:
            return
        info = await asyncio.wait_for(
            lkapi.egress.stop_egress(api.StopEgressRequest(egress_id=egress_id)),
            timeout=5,
        )
        async with session_scope() as db:
            call = await db.get(Call, call_id)
            if call:
                await apply_egress_info(db, call, info)
            await db.commit()
    except Exception:
        logger.warning("recording stop deferred for call_id=%s", call_id, exc_info=True)
