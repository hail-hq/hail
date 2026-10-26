from datetime import datetime, timezone
from uuid import uuid4

import pytest
from hailhq.core.models import Call
from hailhq.core.schemas import CallCreate, CallResponse
from pydantic import ValidationError


def test_call_response_explains_blocked_destinations_without_carrier_errors():
    call = Call(
        id=uuid4(),
        organization_id=uuid4(),
        from_e164="+14155551234",
        to_e164="+12425559999",
        direction="outbound",
        status="failed",
        end_reason="sip_trunk_failure",
        voice_config={},
        requested_at=datetime.now(timezone.utc),
        metadata_={"failure_code": "destination_not_allowed"},
    )
    assert CallResponse.model_validate(call).failure_message == (
        "We cannot call this destination right now. Please try a number in another country or region."
    )


@pytest.mark.parametrize("status", ["dialing", "busy", "completed"])
def test_nonfailed_calls_have_no_failure_message(status):
    call = Call(status=status, metadata_={"failure_code": "destination_not_allowed"})
    assert call.failure_message is None


def test_callers_cannot_supply_a_failure_code():
    with pytest.raises(ValidationError, match="reserved"):
        CallCreate(
            to="+14155551234",
            system_prompt="hello",
            recipient_consent=True,
            metadata={"failure_code": "destination_not_allowed"},
        )


@pytest.mark.parametrize(
    ("end_reason", "expected"),
    [
        ("insufficient_funds", "Your balance is empty."),
        ("no_agent", "This number has no active voice agent."),
        ("provider_key_error", "Your provider API key was rejected."),
        ("llm_endpoint_failed", "Your LLM endpoint kept returning errors"),
        ("carrier_route_failed", "This number cannot carry calls right now."),
        ("sip_trunk_failure", "Please try again in a few minutes."),
        ("sweeper_timeout", "Please try again in a few minutes."),
        (None, "Please try again in a few minutes."),
    ],
)
def test_failure_message_matches_end_reason(end_reason, expected):
    call = Call(status="failed", end_reason=end_reason, metadata_={})
    assert expected in call.failure_message


def test_blocked_destination_wins_over_end_reason():
    call = Call(
        status="failed",
        end_reason="insufficient_funds",
        metadata_={"failure_code": "destination_not_allowed"},
    )
    assert call.failure_message.startswith("We cannot call this destination")
