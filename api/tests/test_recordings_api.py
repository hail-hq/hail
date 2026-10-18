"""Playback is tenant scoped and recording webhook requests are authenticated."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from fastapi import HTTPException
from hailhq.api.routes import recordings
from hailhq.core.config import settings
from hailhq.core.models import Call, PhoneNumber


async def recorded_call(db):
    org = uuid4()
    number = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id="test-recording-number",
        provisioning_state="active",
    )
    db.add(number)
    await db.flush()
    call = Call(
        organization_id=org,
        from_number_id=number.id,
        from_e164="+14155550100",
        to_e164="+14155550101",
        direction="outbound",
        provider="twilio",
        status="completed",
        end_reason="normal_hangup",
        voice_config={},
        ended_at=datetime.now(timezone.utc),
        recording_s3_key="recordings/private/audio.ogg",
        metadata_={"recording_status": "ready"},
    )
    db.add(call)
    await db.commit()
    return call


async def test_playback_is_scoped_to_authenticated_workspace(
    async_session, monkeypatch
):
    call = await recorded_call(async_session)
    signer = AsyncMock(return_value="https://storage.example/audio?signature=temporary")
    monkeypatch.setattr(recordings, "presign_recording", signer)
    with pytest.raises(HTTPException) as error:
        await recordings.get_recording(
            call.id, async_session, SimpleNamespace(organization_id=uuid4())
        )
    assert error.value.status_code == 404
    signer.assert_not_awaited()
    response = await recordings.get_recording(
        call.id, async_session, SimpleNamespace(organization_id=call.organization_id)
    )
    assert response.status_code == 302
    assert response.headers["cache-control"] == "private, no-store"
    signer.assert_awaited_once()


@pytest.mark.parametrize(
    "age_days,retention_days,expected_status",
    [
        (6, 7, 302),
        (8, 7, 404),
        (29, 30, 302),
        (31, 30, 404),
        (364, 365, 302),
        (366, 365, 404),
    ],
)
async def test_recording_playback_honors_retention(
    async_session, monkeypatch, age_days, retention_days, expected_status
):
    monkeypatch.setattr(settings, "hail_recording_retention_days", 365)
    storage = Mock()
    storage.generate_presigned_url.return_value = (
        "https://storage.example/audio?signed=yes"
    )
    monkeypatch.setattr("hailhq.core.recordings.recording_storage", lambda: storage)
    call = await recorded_call(async_session)
    call.metadata_ = {**call.metadata_, "recording_retention_days": retention_days}
    call.ended_at = datetime.now(timezone.utc) - timedelta(days=age_days)
    await async_session.commit()
    principal = SimpleNamespace(organization_id=call.organization_id)
    if expected_status == 404:
        with pytest.raises(HTTPException) as error:
            await recordings.get_recording(call.id, async_session, principal)
        assert error.value.status_code == 404
        storage.generate_presigned_url.assert_not_called()
    else:
        response = await recordings.get_recording(call.id, async_session, principal)
        assert response.status_code == 302
        storage.generate_presigned_url.assert_called_once()


async def test_recording_event_rejects_unsigned_request(client):
    response = await client.post(
        "/v1/livekit/recording-events", json={"event": "egress_ended"}
    )
    assert response.status_code == 401


async def test_signed_url_does_not_outlive_recording_expiry(monkeypatch):
    storage = Mock()
    storage.generate_presigned_url.return_value = (
        "https://storage.example/audio?signed=yes"
    )
    monkeypatch.setattr("hailhq.core.recordings.recording_storage", lambda: storage)
    call = SimpleNamespace(
        recording_s3_key="recordings/7d/private/audio.ogg",
        metadata_={"recording_retention_days": 7},
        ended_at=datetime.now(timezone.utc) - timedelta(days=7) + timedelta(seconds=60),
    )
    await recordings.presign_recording(call)
    assert 1 <= storage.generate_presigned_url.call_args.kwargs["ExpiresIn"] <= 60


async def test_legacy_recording_keeps_original_year_when_service_default_changes(
    monkeypatch,
):
    monkeypatch.setattr(settings, "hail_recording_retention_days", 7)
    storage = Mock()
    storage.generate_presigned_url.return_value = (
        "https://storage.example/audio?signed=yes"
    )
    monkeypatch.setattr("hailhq.core.recordings.recording_storage", lambda: storage)
    call = SimpleNamespace(
        recording_s3_key="recordings/private/audio.ogg",
        metadata_={},
        ended_at=datetime.now(timezone.utc) - timedelta(days=31),
    )
    assert await recordings.presign_recording(call) is not None
    storage.generate_presigned_url.assert_called_once()
