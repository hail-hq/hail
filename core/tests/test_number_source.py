"""PhoneNumberResponse says whether a number was bought or connected."""

from uuid import uuid4

from hailhq.core.models import PhoneNumber
from hailhq.core.schemas import PhoneNumberResponse


def _row(**over):
    fields = {
        "id": uuid4(),
        "organization_id": uuid4(),
        "e164": "+14155550100",
        "country_code": "US",
        "number_type": "local",
        "capabilities": ["voice"],
        "is_pool": False,
        "provisioning_state": "active",
    }
    return PhoneNumber(**{**fields, **over})


def test_number_response_connected_source():
    connection_id = uuid4()
    body = PhoneNumberResponse.model_validate(
        _row(
            provider="pbx",
            sip_connection_id=connection_id,
            provisioning_state="unverified",
        )
    )
    assert body.source == "connected"
    assert body.sip_connection_id == connection_id
    assert body.provisioning_state == "unverified"


def test_number_response_purchased_source():
    body = PhoneNumberResponse.model_validate(_row(provider="twilio"))
    assert (body.source, body.sip_connection_id) == ("purchased", None)
