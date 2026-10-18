"""Recording capacity and billing must never control the live call."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.config import settings
from hailhq.core.models import Call, UsageEvent
from hailhq.core.recordings import apply_egress_info, recording_enabled
from hailhq.voicebot.agent import on_call_end
from hailhq.voicebot.recording import start_recording, stop_recording
from livekit import api
from sqlalchemy import select

from .test_agent import _make_call_row


@pytest.mark.parametrize(
    "workspace,override,expected",
    [
        (None, True, False),
        (False, True, False),
        (True, None, True),
        (True, False, False),
        (True, True, True),
    ],
)
def test_recording_permission(workspace, override, expected):
    assert recording_enabled(workspace, override) is expected


async def test_exhausted_capacity_keeps_call_and_combined_charge(
    async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_recording_enabled", True)
    call_id = await _make_call_row(async_session)
    call = await async_session.get(Call, call_id)
    call.metadata_ = {"recording_enabled": True, "recording_status": "requested"}
    call.answered_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    await async_session.commit()
    lk = SimpleNamespace(
        egress=SimpleNamespace(
            start_room_composite_egress=AsyncMock(
                side_effect=RuntimeError("quota exhausted")
            )
        )
    )
    await start_recording(call_id, f"hail-{call_id}", lk)
    await async_session.refresh(call)
    assert call.metadata_["recording_status"] == "unavailable"
    assert call.status == "dialing"
    await on_call_end(call_id, f"hail-{call_id}")
    await on_call_end(call_id, f"hail-{call_id}")
    rows = (
        (
            await async_session.execute(
                select(UsageEvent).where(UsageEvent.ref == f"{call_id}:recording-v1")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].units >= 30_000
    assert lk.egress.start_room_composite_egress.await_count == 1


async def test_disabled_workspace_never_starts_egress(async_session, monkeypatch):
    monkeypatch.setattr(settings, "hail_recording_enabled", True)
    call_id = await _make_call_row(async_session)
    lk = SimpleNamespace(
        egress=SimpleNamespace(start_room_composite_egress=AsyncMock())
    )
    await start_recording(call_id, f"hail-{call_id}", lk)
    lk.egress.start_room_composite_egress.assert_not_awaited()


async def test_completed_recording_does_not_replace_call_status(async_session):
    call_id = await _make_call_row(async_session)
    call = await async_session.get(Call, call_id)
    call.livekit_room = f"hail-{call_id}"
    call.metadata_ = {
        "recording_egress_id": "egress-1",
        "recording_object_key": "recordings/org/call/audio.ogg",
        "recording_status": "processing",
    }
    await async_session.commit()
    info = api.EgressInfo(
        egress_id="egress-1",
        room_name=call.livekit_room,
        status=api.EGRESS_COMPLETE,
        file_results=[
            api.FileInfo(
                filename="untrusted-path",
                size=12000,
                duration=30_000_000_000,
                started_at=1_800_000_000_000_000_000,
            )
        ],
    )
    await apply_egress_info(async_session, call, info)
    await async_session.commit()
    await async_session.refresh(call)
    assert call.recording_s3_key == "recordings/org/call/audio.ogg"
    assert call.recording_duration_ms == 30_000
    assert call.recording_status == "ready"
    assert call.status == "dialing"


async def test_late_upload_cannot_restore_erased_recording(async_session, monkeypatch):
    from unittest.mock import Mock

    from hailhq.core.recordings import delete_recording, recording_state

    storage = Mock()
    monkeypatch.setattr("hailhq.core.recordings.recording_storage", lambda: storage)
    call_id = await _make_call_row(async_session)
    call = await async_session.get(Call, call_id)
    call.livekit_room = f"hail-{call_id}"
    call.metadata_ = {
        "recording_enabled": True,
        "recording_egress_id": "egress-erased",
        "recording_object_key": "recordings/org/call/audio.ogg",
        "recording_status": "processing",
    }
    await async_session.commit()
    await delete_recording(call)
    await async_session.commit()
    await recording_state(async_session, call_id, recording_status="recording")
    await async_session.commit()
    await async_session.refresh(call)
    assert call.recording_status == "erased"
    await apply_egress_info(
        async_session,
        call,
        api.EgressInfo(
            egress_id="egress-erased",
            room_name=call.livekit_room,
            status=api.EGRESS_COMPLETE,
            file_results=[api.FileInfo(size=1200, duration=1_000_000_000)],
        ),
    )
    await async_session.commit()
    await async_session.refresh(call)
    assert call.recording_s3_key is None
    assert call.recording_status == "erased"
    assert storage.delete_object.call_count == 2


@pytest.mark.parametrize("days", [7, 30, 365])
async def test_recording_uses_snapshotted_storage_expiry(
    async_session, monkeypatch, days
):
    monkeypatch.setattr(settings, "hail_recording_enabled", True)
    monkeypatch.setattr(settings, "hail_recording_retention_days", 365)
    call_id = await _make_call_row(async_session)
    call = await async_session.get(Call, call_id)
    call.livekit_room = f"hail-{call_id}"
    call.metadata_ = {
        "recording_enabled": True,
        "recording_status": "requested",
        "recording_retention_days": days,
    }
    await async_session.commit()
    lk = SimpleNamespace(
        egress=SimpleNamespace(
            start_room_composite_egress=AsyncMock(
                return_value=api.EgressInfo(
                    egress_id="egress-retention",
                    room_name=call.livekit_room,
                    status=api.EGRESS_ACTIVE,
                )
            )
        )
    )
    await start_recording(call_id, call.livekit_room, lk)
    request = lk.egress.start_room_composite_egress.await_args.args[0]
    assert request.file_outputs[0].filepath.startswith(f"recordings/{days}d/")


async def test_cancelled_start_never_leaves_recording_starting(async_session):
    """A start that died before LiveKit answered has no Egress id to reconcile,
    so the stop path settles the status instead of leaving it "starting"."""
    call_id = await _make_call_row(async_session)
    call = await async_session.get(Call, call_id)
    call.metadata_ = {
        "recording_enabled": True,
        "recording_status": "starting",
        "recording_object_key": "recordings/365d/org/call/audio.ogg",
    }
    await async_session.commit()
    lk = SimpleNamespace(egress=SimpleNamespace(stop_egress=AsyncMock()))
    await stop_recording(call_id, lk)
    await async_session.refresh(call)
    assert call.recording_status == "unavailable"
    lk.egress.stop_egress.assert_not_awaited()


async def test_cancelled_start_recovers_egress_by_room(async_session):
    """LiveKit may have started the Egress although our start task was
    cancelled before the id was stored; stop it instead of orphaning it."""
    call_id = await _make_call_row(async_session)
    call = await async_session.get(Call, call_id)
    call.livekit_room = f"hail-{call_id}"
    call.metadata_ = {
        "recording_enabled": True,
        "recording_status": "starting",
        "recording_object_key": "recordings/365d/org/call/audio.ogg",
    }
    await async_session.commit()
    info = api.EgressInfo(
        egress_id="egress-orphan", room_name=call.livekit_room, status=api.EGRESS_ACTIVE
    )
    lk = SimpleNamespace(
        egress=SimpleNamespace(
            list_egress=AsyncMock(return_value=SimpleNamespace(items=[info])),
            stop_egress=AsyncMock(
                return_value=api.EgressInfo(
                    egress_id="egress-orphan",
                    room_name=call.livekit_room,
                    status=api.EGRESS_ENDING,
                )
            ),
        )
    )
    await stop_recording(call_id, lk, call.livekit_room)
    await async_session.refresh(call)
    assert call.metadata_["recording_egress_id"] == "egress-orphan"
    assert call.recording_status != "unavailable"
    lk.egress.stop_egress.assert_awaited_once()
