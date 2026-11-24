from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from hailhq.core.models import (
    Call,
    CallLeg,
    Contact,
    PhoneNumber,
    SipConnection,
    Sms,
)
from sqlalchemy.exc import IntegrityError


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


async def test_channel_suspension_unique_per_org_and_channel(async_session) -> None:
    import uuid

    from hailhq.core.models import ChannelSuspension
    from sqlalchemy.exc import IntegrityError

    org_id = uuid.uuid4()
    async_session.add(
        ChannelSuspension(organization_id=org_id, channel="sms", reason="a")
    )
    await async_session.commit()

    async_session.add(
        ChannelSuspension(organization_id=org_id, channel="sms", reason="b")
    )
    with pytest.raises(IntegrityError):
        await async_session.commit()


ORG = uuid.UUID("00000000-0000-0000-0000-0000000000a1")
T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def number_row(e164, provider="twilio", sip_connection_id=None, **over):
    return PhoneNumber(
        organization_id=ORG,
        e164=e164,
        country_code="US",
        number_type="local",
        capabilities=["voice"],
        provider=provider,
        sip_connection_id=sip_connection_id,
        **over,
    )


def pbx_row(e164, sip_connection_id=None, **over):
    return number_row(e164, "pbx", sip_connection_id, **over)


@pytest.fixture()
async def conn(async_session):
    row = SipConnection(
        organization_id=ORG,
        label="Main office",
        inbound_username="pbx_user_1",
        encrypted_inbound_password="token",
        default_region="US",
    )
    async_session.add(row)
    await async_session.commit()
    return row


@pytest.fixture()
async def pbx_call(async_session, conn):
    number = pbx_row("+14155550199", conn.id, provisioning_state="active")
    async_session.add(number)
    await async_session.flush()
    call = Call(
        organization_id=ORG,
        from_number_id=number.id,
        from_e164="+14155550123",
        to_e164=number.e164,
        voice_config={},
    )
    async_session.add(call)
    await async_session.commit()
    return call


async def test_sip_connection_defaults(conn):
    assert conn.pbx_port == 5060
    assert conn.pbx_transport == "auto"
    assert conn.pbx_host is None


async def test_sip_connection_constraints(async_session, conn):
    def make(**over):
        fields = {
            "organization_id": ORG,
            "label": "Other",
            "inbound_username": "pbx_user_2",
            "encrypted_inbound_password": "token",
            "default_region": "US",
        }
        fields.update(over)
        return SipConnection(**fields)

    for over, name in [
        ({"label": ""}, "sip_connections_label_len"),
        ({"label": "x" * 81}, "sip_connections_label_len"),
        ({"pbx_transport": "ws"}, "sip_connections_transport_check"),
        ({"pbx_port": 0}, "sip_connections_port_check"),
        ({"pbx_port": 65536}, "sip_connections_port_check"),
        ({"inbound_username": "pbx_user_1"}, "sip_connections_inbound_username_key"),
    ]:
        async_session.add(make(**over))
        with pytest.raises(IntegrityError, match=name):
            await async_session.flush()
        await async_session.rollback()


async def test_pbx_number_needs_connection(async_session):
    async_session.add(pbx_row("+14155550100", sip_connection_id=None))
    with pytest.raises(IntegrityError, match="phone_numbers_pbx_connection_check"):
        await async_session.flush()


async def test_carrier_number_cannot_have_connection(async_session, conn):
    async_session.add(
        number_row("+14155550100", provider="twilio", sip_connection_id=conn.id)
    )
    with pytest.raises(IntegrityError, match="phone_numbers_pbx_connection_check"):
        await async_session.flush()


async def test_unverified_state_is_allowed_and_holds_e164(async_session, conn):
    first = pbx_row("+14155550100", conn.id, provisioning_state="unverified")
    async_session.add(first)
    await async_session.commit()
    assert first.provisioning_state == "unverified"
    async_session.add(pbx_row("+14155550100", conn.id, provisioning_state="unverified"))
    with pytest.raises(IntegrityError, match="phone_numbers_e164_live_uniq"):
        await async_session.flush()


async def test_contact_with_only_extension(async_session):
    ok = Contact(organization_id=ORG, name="Support line", extension="800")
    async_session.add(ok)
    await async_session.commit()
    assert ok.extension == "800"
    async_session.add(Contact(organization_id=ORG, name="Bad", extension="80a"))
    with pytest.raises(IntegrityError, match="contacts_extension_format"):
        await async_session.flush()
    await async_session.rollback()
    async_session.add(Contact(organization_id=ORG, name="Empty"))
    with pytest.raises(IntegrityError, match="contacts_phone_email_or_extension"):
        await async_session.flush()


async def test_call_leg_accepts_pbx(async_session, pbx_call):
    leg = CallLeg(
        call_id=pbx_call.id,
        role="call",
        provider="pbx",
        direction="inbound",
        joined_at=T0,
        cost_status="found",
        cost_usd=Decimal(0),
    )
    async_session.add(leg)
    await async_session.commit()
    assert leg.provider == "pbx"
