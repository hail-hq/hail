"""Recording startup and erasure share the call's transaction lock."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from hailhq.core.config import settings
from hailhq.core.models import Call
from hailhq.voicebot import recording
from livekit import api


@pytest.fixture
def recording_db(monkeypatch):
    """Model transaction row locks without a database or LiveKit service."""
    call_id = uuid4()
    store = SimpleNamespace(
        call=SimpleNamespace(
            id=call_id,
            organization_id=uuid4(),
            status="in_progress",
            livekit_room=f"hail-{call_id}",
            metadata_={"recording_enabled": True, "recording_status": "requested"},
        ),
        lock=asyncio.Lock(),
        after_commit=None,
    )

    class Session:
        def __init__(self):
            self.call = None
            self.locked = False
            self.recording_values = {}

        async def get(self, model, identifier, **kwargs):
            assert model is Call and identifier == call_id
            if kwargs.get("with_for_update") and not self.locked:
                await store.lock.acquire()
                self.locked = True
            if self.call is None or kwargs.get("populate_existing"):
                self.call = deepcopy(store.call)
                self.call.metadata_ = {
                    **self.call.metadata_,
                    **self.recording_values,
                }
            return self.call

        def release(self):
            if self.locked:
                self.locked = False
                store.lock.release()

        async def commit(self):
            if self.call is not None:
                store.call = deepcopy(self.call)
            self.release()
            hook, store.after_commit = store.after_commit, None
            if hook:
                await hook()

    @asynccontextmanager
    async def session_scope():
        db = Session()
        try:
            yield db
        finally:
            db.release()

    async def recording_state(db, identifier, **values):
        call = await db.get(Call, identifier)
        if (
            "recording_status" not in values
            or call.metadata_.get("recording_status") != "erased"
        ):
            db.recording_values.update(values)
            call.metadata_ = {**call.metadata_, **values}

    monkeypatch.setattr(recording, "session_scope", session_scope)
    monkeypatch.setattr(recording, "recording_state", recording_state)
    monkeypatch.setattr("hailhq.core.recordings.recording_state", recording_state)
    monkeypatch.setattr(settings, "hail_recording_enabled", True)
    monkeypatch.setattr(settings, "s3_bucket", "recording-test-bucket")
    store.session_scope = session_scope
    return store


@pytest.mark.parametrize(
    "status", ["completed", "failed", "busy", "no_answer", "canceled"]
)
async def test_terminal_call_never_starts_recording(recording_db, status):
    recording_db.call.status = status
    uploads = []

    async def start(request):
        uploads.append(request.file_outputs[0].filepath)
        return api.EgressInfo(egress_id="unexpected", status=api.EGRESS_ACTIVE)

    lk = SimpleNamespace(egress=SimpleNamespace(start_room_composite_egress=start))
    await recording.start_recording(
        recording_db.call.id, recording_db.call.livekit_room, lk
    )

    assert uploads == []
    assert recording_db.call.metadata_["recording_status"] == "requested"


async def test_erasure_between_reservation_and_start_prevents_upload(recording_db):
    reserved = asyncio.Event()
    erased = asyncio.Event()
    uploads = []

    async def pause_after_reservation():
        reserved.set()
        await erased.wait()

    async def erase():
        await reserved.wait()
        async with recording_db.session_scope() as db:
            call = await db.get(Call, recording_db.call.id, with_for_update=True)
            assert call.metadata_["recording_object_key"]
            call.status = "completed"
            call.metadata_ = {**call.metadata_, "recording_status": "erased"}
            await db.commit()
        erased.set()

    async def start(request):
        uploads.append(request.file_outputs[0].filepath)
        return api.EgressInfo(egress_id="too-late", status=api.EGRESS_ACTIVE)

    recording_db.after_commit = pause_after_reservation
    lk = SimpleNamespace(egress=SimpleNamespace(start_room_composite_egress=start))
    await asyncio.wait_for(
        asyncio.gather(
            recording.start_recording(
                recording_db.call.id, recording_db.call.livekit_room, lk
            ),
            erase(),
        ),
        timeout=1,
    )

    assert uploads == []
    assert recording_db.call.metadata_["recording_status"] == "erased"


async def test_erasure_waits_for_provider_start_and_reads_egress_id(recording_db):
    starting = asyncio.Event()
    finish_start = asyncio.Event()
    cleanup_attempted = asyncio.Event()
    cleanup_snapshots = []

    async def start(request):
        starting.set()
        await finish_start.wait()
        return api.EgressInfo(
            egress_id="egress-before-erasure",
            room_name=recording_db.call.livekit_room,
            status=api.EGRESS_ACTIVE,
        )

    async def erase():
        await starting.wait()
        async with recording_db.session_scope() as db:
            cleanup_attempted.set()
            call = await db.get(
                Call, recording_db.call.id, with_for_update=True, populate_existing=True
            )
            cleanup_snapshots.append(deepcopy(call.metadata_))
            call.metadata_ = {**call.metadata_, "recording_status": "erased"}
            await db.commit()

    lk = SimpleNamespace(egress=SimpleNamespace(start_room_composite_egress=start))
    tasks = [
        asyncio.create_task(
            recording.start_recording(
                recording_db.call.id, recording_db.call.livekit_room, lk
            )
        ),
        asyncio.create_task(erase()),
    ]
    try:
        await asyncio.wait_for(cleanup_attempted.wait(), timeout=1)
        assert cleanup_snapshots == [], "erasure crossed an in-flight provider start"
    finally:
        finish_start.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)

    assert cleanup_snapshots[0]["recording_egress_id"] == "egress-before-erasure"
    assert recording_db.call.metadata_["recording_status"] == "erased"
