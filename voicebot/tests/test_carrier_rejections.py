"""Disambiguate carrier failures from LiveKit's USER_REJECTED reason."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from hailhq.core.models import (
    Call,
    PhoneNumber,
    UsageEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.providers.voice.base import ProviderCallStatus
from hailhq.core.schemas import CallResponse
from hailhq.voicebot import agent
from livekit import rtc
from sqlalchemy import select

from ._fakes import FakeJobContext, FakeParticipant

CALL_ID = UUID("8883e8af-facc-496a-b1ae-7530da41fc34")
TWILIO_SID = "CA673a5bd72e11e57fd31d306eba071d7a"


@pytest.mark.parametrize(
    "carrier_status,expected_status,expected_reason",
    [
        ("failed", "failed", "sip_trunk_failure"),
        ("busy", "busy", "user_rejected"),
        ("queued", "busy", "user_rejected"),
    ],
)
async def test_rejected_sip_leg_uses_twilio_outcome(
    monkeypatch, carrier_status, expected_status, expected_reason
):
    captured = {"status": None, "end_reason": None}
    participant = FakeParticipant("caller", rtc.DisconnectReason.USER_REJECTED)
    participant.attributes = {"sip.twilio.callSid": TWILIO_SID}
    get_status = AsyncMock(
        return_value=ProviderCallStatus(
            provider_call_sid=TWILIO_SID, status=carrier_status
        )
    )
    monkeypatch.setattr(
        agent,
        "TwilioVoiceProvider",
        lambda **kw: SimpleNamespace(
            get_call_status=get_status, get_call_error_code=AsyncMock(return_value=None)
        ),
    )
    write_event = AsyncMock()
    monkeypatch.setattr(agent, "write_call_event", write_event)

    agent.handle_sip_disconnect(FakeJobContext(), participant, captured, {}, CALL_ID)
    await agent.reconcile_sip_rejection(CALL_ID, captured)

    get_status.assert_awaited_once_with(TWILIO_SID)
    assert captured["status"] == expected_status
    assert captured["end_reason"] == expected_reason
    write_event.assert_awaited_once_with(
        CALL_ID,
        "connection_result",
        {
            "status": expected_status,
            **(
                {
                    "failure_message": "We could not complete your call. Please try again in a few minutes."
                }
                if expected_status == "failed"
                else {}
            ),
        },
    )


@pytest.mark.parametrize("error", [RuntimeError("carrier unavailable"), TimeoutError()])
async def test_carrier_lookup_failure_keeps_finalization_possible(monkeypatch, error):
    captured = {
        "status": "busy",
        "end_reason": "user_rejected",
        "twilio_call_sid": TWILIO_SID,
    }
    get_status = AsyncMock(side_effect=error)
    monkeypatch.setattr(
        agent,
        "TwilioVoiceProvider",
        lambda **kw: SimpleNamespace(get_call_status=get_status),
    )
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    assert captured["status"] == "busy"
    assert captured["end_reason"] == "user_rejected"


async def test_later_call_outcome_wins_over_carrier_lookup(monkeypatch):
    captured = {
        "status": "busy",
        "end_reason": "user_rejected",
        "twilio_call_sid": TWILIO_SID,
    }

    async def get_status(sid):
        captured.update(status="failed", end_reason="agent_error")
        return ProviderCallStatus(provider_call_sid=sid, status="failed")

    monkeypatch.setattr(
        agent,
        "TwilioVoiceProvider",
        lambda **kw: SimpleNamespace(get_call_status=get_status),
    )
    monkeypatch.setattr(agent, "write_call_event", AsyncMock())
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    assert captured["end_reason"] == "agent_error"


async def test_destination_block_has_a_safe_human_readable_failure(monkeypatch):
    captured = {
        "status": "busy",
        "end_reason": "user_rejected",
        "twilio_call_sid": TWILIO_SID,
    }
    provider = SimpleNamespace(
        get_call_status=AsyncMock(
            return_value=ProviderCallStatus(
                provider_call_sid=TWILIO_SID, status="failed"
            )
        ),
        get_call_error_code=AsyncMock(return_value="32205"),
    )
    monkeypatch.setattr(agent, "TwilioVoiceProvider", lambda **kw: provider)
    monkeypatch.setattr(agent, "write_call_event", AsyncMock())
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    assert captured["status"] == "failed"
    assert captured["failure_code"] == "destination_not_allowed"


async def test_diagnostic_timeout_does_not_lose_a_confirmed_failure(monkeypatch):
    captured = {
        "status": "busy",
        "end_reason": "user_rejected",
        "twilio_call_sid": TWILIO_SID,
    }
    provider = SimpleNamespace(
        get_call_status=AsyncMock(
            return_value=ProviderCallStatus(
                provider_call_sid=TWILIO_SID, status="failed"
            )
        ),
        get_call_error_code=AsyncMock(side_effect=TimeoutError()),
    )
    monkeypatch.setattr(agent, "TwilioVoiceProvider", lambda **kw: provider)
    monkeypatch.setattr(agent, "write_call_event", AsyncMock())
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    assert captured["status"] == "failed"
    assert captured["end_reason"] == "sip_trunk_failure"


async def test_later_sip_failure_discards_stale_destination_diagnosis(monkeypatch):
    captured = {
        "status": "busy",
        "end_reason": "user_rejected",
        "twilio_call_sid": TWILIO_SID,
    }
    provider = SimpleNamespace(
        get_call_status=AsyncMock(
            return_value=ProviderCallStatus(
                provider_call_sid=TWILIO_SID, status="failed"
            )
        ),
        get_call_error_code=AsyncMock(return_value="32205"),
    )
    monkeypatch.setattr(agent, "TwilioVoiceProvider", lambda **kw: provider)

    async def write_event(*args):
        participant = FakeParticipant("caller", rtc.DisconnectReason.MEDIA_FAILURE)
        agent.handle_sip_disconnect(
            FakeJobContext(), participant, captured, {}, CALL_ID
        )

    monkeypatch.setattr(agent, "write_call_event", write_event)
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    assert captured["end_reason"] == "media_failure"
    assert "failure_code" not in captured
    assert "twilio_call_sid" not in captured


@pytest.mark.parametrize(
    "captured",
    [
        {"status": "busy", "end_reason": "user_rejected"},
        {
            "status": "failed",
            "end_reason": "agent_error",
            "twilio_call_sid": TWILIO_SID,
        },
        {"status": None, "end_reason": None},
    ],
)
async def test_unrelated_outcomes_do_not_query_twilio(monkeypatch, captured):
    provider = AsyncMock()
    monkeypatch.setattr(agent, "TwilioVoiceProvider", provider)
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    provider.assert_not_called()


@pytest.mark.parametrize("later_reason", [None, "media_failure"])
async def test_failed_rejection_emits_failed_webhook_without_usage(
    async_session, monkeypatch, later_reason
):
    org_id = UUID("aaaaaaaa-2222-3333-4444-555555555555")
    number = PhoneNumber(
        organization_id=org_id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.flush()
    call = Call(
        id=CALL_ID,
        organization_id=org_id,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+12427888000",
        voice_config={},
        status="dialing",
        started_at=datetime.now(timezone.utc),
    )
    async_session.add(call)
    async_session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/events",
            secret_encrypted="test",
            event_types=["call.failed", "call.busy"],
        )
    )
    await async_session.commit()
    monkeypatch.setattr(
        agent,
        "TwilioVoiceProvider",
        lambda **kw: SimpleNamespace(
            get_call_status=AsyncMock(
                return_value=ProviderCallStatus(
                    provider_call_sid=TWILIO_SID, status="failed"
                )
            ),
            get_call_error_code=AsyncMock(return_value="32205"),
        ),
    )
    captured = {"status": None, "end_reason": None}
    participant = FakeParticipant("caller", rtc.DisconnectReason.USER_REJECTED)
    participant.attributes = {"sip.twilio.callSid": TWILIO_SID}
    agent.handle_sip_disconnect(FakeJobContext(), participant, captured, {}, CALL_ID)
    await agent.reconcile_sip_rejection(CALL_ID, captured)
    if later_reason:
        captured["end_reason"] = later_reason
    await agent.on_call_end(
        CALL_ID,
        f"hail-{CALL_ID}",
        captured["status"],
        captured["end_reason"],
        failure_code=captured.get("failure_code"),
    )

    await async_session.refresh(call)
    assert (call.status, call.end_reason) == (
        "failed",
        later_reason or "sip_trunk_failure",
    )
    assert call.answered_at is None
    message = (
        "We could not complete your call. Please try again in a few minutes."
        if later_reason
        else "We cannot call this destination right now. Please try a number in another country or region."
    )
    assert CallResponse.model_validate(call).failure_message == message
    assert (await async_session.execute(select(UsageEvent))).scalars().all() == []
    deliveries = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert [delivery.event_type for delivery in deliveries] == ["call.failed"]
    assert deliveries[0].payload["data"]["failure_message"] == message
