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
        "Calling this destination is currently disabled. Try another destination or contact support."
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
