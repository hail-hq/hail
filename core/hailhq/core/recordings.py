"""Recording state and signed playback, independent of the cloud rater."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from uuid import UUID

import boto3
from hailhq.core.config import settings
from hailhq.core.models import Call
from livekit import api
from sqlalchemy import cast, update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession


def recording_enabled(workspace: bool | None, override: bool | None = None) -> bool:
    """The workspace is the permission boundary; per-call/agent can opt out."""
    return workspace is True and override is not False


def recording_retention_days(call: Call) -> int:
    """Use the call snapshot; changing workspace settings never changes old audio."""
    days = (call.metadata_ or {}).get("recording_retention_days")
    return days if type(days) is int and days in (7, 30, 365) else 365


def recording_storage():
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint or None,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key or None,
        aws_secret_access_key=settings.s3_secret_key or None,
    )


async def recording_state(db: AsyncSession, call_id: UUID, **values) -> None:
    statement = update(Call).where(Call.id == call_id)
    if "recording_status" in values:
        # A late Egress response must never undo an erasure tombstone.
        statement = statement.where(
            Call.metadata_["recording_status"].astext.is_distinct_from("erased")
        )
    await db.execute(
        statement.values(metadata_=Call.metadata_.op("||")(cast(values, JSONB)))
    )


async def apply_egress_info(db: AsyncSession, call: Call, info: api.EgressInfo) -> None:
    """Idempotent reconciliation. Never use a vendor-supplied object key."""
    meta = call.metadata_ or {}
    if (
        meta.get("recording_egress_id") != info.egress_id
        or info.room_name != call.livekit_room
    ):
        return
    if meta.get("recording_status") == "erased":
        # Uploads can finish after a deletion request. Remove that object too.
        if info.status in {
            api.EGRESS_COMPLETE,
            api.EGRESS_LIMIT_REACHED,
            api.EGRESS_FAILED,
            api.EGRESS_ABORTED,
        }:
            await delete_recording(call)
        return
    if meta.get("recording_status") in {"ready", "failed"}:
        return
    file = info.file_results[0] if info.file_results else None
    started = (file.started_at if file and file.started_at else info.started_at) / 1e9
    values = {"recording_status": "processing"}
    if started > 0:
        values["recording_started_at"] = started
    if info.status == api.EGRESS_ACTIVE:
        values["recording_status"] = "recording"
    elif info.status in {api.EGRESS_COMPLETE, api.EGRESS_LIMIT_REACHED}:
        if (
            file
            and file.size > 0
            and file.duration > 0
            and meta.get("recording_object_key")
        ):
            await db.execute(
                update(Call)
                .where(
                    Call.id == call.id,
                    Call.metadata_["recording_status"].astext.is_distinct_from(
                        "erased"
                    ),
                )
                .values(
                    recording_s3_key=meta["recording_object_key"],
                    recording_duration_ms=file.duration // 1_000_000,
                )
            )
            values["recording_status"] = "ready"
            values["recording_partial"] = info.status == api.EGRESS_LIMIT_REACHED
        else:
            values["recording_status"] = "failed"
    elif info.status in {api.EGRESS_FAILED, api.EGRESS_ABORTED}:
        values["recording_status"] = "failed"
    await recording_state(db, call.id, **values)


async def presign_recording(call: Call) -> str | None:
    if (
        not call.recording_s3_key
        or (call.metadata_ or {}).get("recording_status") == "erased"
    ):
        return None
    expires_in = 900
    if call.ended_at:
        remaining = (
            recording_retention_days(call) * 86400
            - (datetime.now(timezone.utc) - call.ended_at).total_seconds()
        )
        if remaining <= 0:
            return None
        expires_in = min(expires_in, max(1, int(remaining)))
    return await asyncio.to_thread(
        recording_storage().generate_presigned_url,
        "get_object",
        Params={"Bucket": settings.s3_bucket, "Key": call.recording_s3_key},
        ExpiresIn=expires_in,
    )


async def delete_recording(call: Call) -> None:
    """Delete the object before clearing its reference. S3 delete is idempotent."""
    key = call.recording_s3_key or (call.metadata_ or {}).get("recording_object_key")
    if key:
        await asyncio.to_thread(
            recording_storage().delete_object,
            Bucket=settings.s3_bucket,
            Key=key,
        )
    call.recording_s3_key = None
    call.recording_duration_ms = None
    call.metadata_ = {**(call.metadata_ or {}), "recording_status": "erased"}
