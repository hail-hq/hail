from datetime import datetime

from hailhq.core.models import Call, Organization, PhoneNumber


def test_call_round_trip(session):
    org = Organization(name="Acme", slug="acme")
    session.add(org)
    session.flush()

    number = PhoneNumber(
        organization_id=org.id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN123",
    )
    session.add(number)
    session.flush()

    call = Call(
        organization_id=org.id,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+14155559999",
        voice_config={"stt": "deepgram", "tts": "elevenlabs"},
    )
    session.add(call)
    session.commit()

    fetched = session.get(Call, call.id)
    assert fetched is not None
    assert fetched.to_e164 == "+14155559999"
    assert fetched.status == "queued"
    assert isinstance(fetched.created_at, datetime)
