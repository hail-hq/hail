"""Voice-only number + SMS number: call, text, call again."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import threads
from hailhq.core.models import Agent, Call, CallEvent, PhoneNumber
from hailhq.core.sms_ingest import ingest_inbound_sms

VOICE_NUMBER = "+33100000001"
SMS_NUMBER = "+33100000002"
OTHER_SMS_NUMBER = "+33100000003"
PERSON = "+33612345678"


async def _agent(session, org, name):
    agent = Agent(organization_id=org, name=name, system_prompt="Help.")
    session.add(agent)
    await session.flush()
    return agent


async def _number(session, org, e164, cap, agent_id) -> PhoneNumber:
    number = PhoneNumber(
        organization_id=org,
        e164=e164,
        country_code="FR",
        number_type="local",
        provisioning_state="active",
        provider_resource_id=e164,
        capabilities=[cap],
        **{f"{cap}_agent_id": agent_id},
    )
    session.add(number)
    await session.flush()
    return number


async def _inbound_call(
    session, org, agent, number, caller, *, status, turn=None
) -> Call:
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        from_e164=caller,
        to_e164=number.e164,
        direction="inbound",
        status=status,
        end_reason="normal_hangup" if status == "completed" else None,
        provider="twilio",
        voice_config={},
    )
    session.add(call)
    await session.flush()
    if turn is not None:
        session.add(
            CallEvent(
                call_id=call.id,
                kind="user_turn",
                payload={"role": "user", "text": turn},
                occurred_at=datetime.now(timezone.utc) - timedelta(minutes=5),
            )
        )
    await session.flush()
    return call


async def _text(session, to_e164, caller, body, sid):
    await ingest_inbound_sms(
        session,
        from_e164=caller,
        to_e164=to_e164,
        body=body,
        provider_message_sid=sid,
        opt_out_type=None,
        carrier="twilio",
    )


async def test_text_on_the_sms_number_shows_in_the_next_calls_thread(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org, "Desk")
    voice = await _number(async_session, org, VOICE_NUMBER, "voice", agent.id)
    await _number(async_session, org, SMS_NUMBER, "sms", agent.id)
    await _inbound_call(
        async_session,
        org,
        agent,
        voice,
        PERSON,
        status="completed",
        turn="I will text you my order",
    )
    await async_session.commit()

    await _text(async_session, SMS_NUMBER, PERSON, "order 4411", "SMX")
    second = await _inbound_call(
        async_session, org, agent, voice, PERSON, status="in_progress"
    )
    await async_session.commit()

    key = await threads.call_thread_key(async_session, second.id)
    items = await threads.thread_items(async_session, *key)

    assert [i.text for i in items] == ["I will text you my order", "order 4411"]


async def test_text_to_another_agents_number_stays_out_of_the_thread(async_session):
    org = uuid.uuid4()
    desk = await _agent(async_session, org, "Desk")
    other = await _agent(async_session, org, "Other")
    voice = await _number(async_session, org, VOICE_NUMBER, "voice", desk.id)
    await _number(async_session, org, SMS_NUMBER, "sms", desk.id)
    await _number(async_session, org, OTHER_SMS_NUMBER, "sms", other.id)
    call = await _inbound_call(
        async_session, org, desk, voice, PERSON, status="in_progress"
    )
    await async_session.commit()

    await _text(async_session, OTHER_SMS_NUMBER, PERSON, "private to other", "SMY")
    await _text(async_session, SMS_NUMBER, PERSON, "for desk", "SMZ")
    await async_session.commit()

    key = await threads.call_thread_key(async_session, call.id)
    items = await threads.thread_items(async_session, *key)

    assert [i.text for i in items] == ["for desk"]


async def test_anonymous_caller_gets_no_thread(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org, "Desk")
    voice = await _number(async_session, org, VOICE_NUMBER, "voice", agent.id)
    await _number(async_session, org, SMS_NUMBER, "sms", agent.id)
    call = await _inbound_call(
        async_session, org, agent, voice, "anonymous", status="in_progress"
    )
    await async_session.commit()

    await _text(async_session, SMS_NUMBER, "anonymous", "hello", "SMA")
    await async_session.commit()

    assert await threads.call_thread_key(async_session, call.id) is None
    assert await threads.thread_items(async_session, org, agent.id, "anonymous") == []
