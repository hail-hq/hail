from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy.exc import IntegrityError

from hailhq.core.models import Call, PhoneNumber, Sms


def test_call_round_trip(session):
    org_id = uuid.uuid4()

    number = PhoneNumber(
        organization_id=org_id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN123",
    )
    session.add(number)
    session.flush()

    call = Call(
        organization_id=org_id,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+14155559999",
        voice_config={"stt": "deepgram", "tts": "cartesia"},
    )
    session.add(call)
    session.commit()

    fetched = session.get(Call, call.id)
    assert fetched is not None
    assert fetched.to_e164 == "+14155559999"
    assert fetched.status == "queued"
    assert isinstance(fetched.created_at, datetime)


async def _make_phone_number(session, organization_id: uuid.UUID) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()
    return pn


async def test_sms_insert_defaults(async_session) -> None:
    org_id = uuid.uuid4()
    pn = await _make_phone_number(async_session, org_id)

    sms = Sms(
        organization_id=org_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155559999",
        body="hi",
    )
    async_session.add(sms)
    await async_session.commit()
    await async_session.refresh(sms)

    assert sms.direction == "outbound"
    assert sms.status == "queued"
    assert sms.segment_count == 1
    assert sms.provider == "twilio"
    assert sms.metadata_ == {}


async def test_sms_status_check_constraint(async_session) -> None:
    org_id = uuid.uuid4()
    pn = await _make_phone_number(async_session, org_id)

    sms = Sms(
        organization_id=org_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155559999",
        body="hi",
        status="not_a_real_status",
    )
    async_session.add(sms)
    with pytest.raises(IntegrityError):
        await async_session.commit()
