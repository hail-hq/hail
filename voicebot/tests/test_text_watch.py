"""Mid-call texts reach the live voice session."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import threads
from hailhq.core.models import Agent, Call, PhoneNumber, Sms
from hailhq.voicebot import text_watch

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


class FakeSession:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def generate_reply(self, **kwargs) -> None:
        self.inputs.append(kwargs["user_input"])


async def _seed_call(async_session):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="x")
    async_session.add(agent)
    await async_session.flush()
    number = PhoneNumber(
        organization_id=org,
        e164=ORG_NUMBER,
        country_code="US",
        number_type="local",
        provider="twilio",
        provisioning_state="active",
    )
    async_session.add(number)
    await async_session.flush()
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status="in_progress",
        provider="twilio",
        voice_config={},
    )
    async_session.add(call)
    await async_session.commit()
    return org, agent, call


def _text(org, agent_id, body, *, person=PERSON, at=None):
    return Sms(
        organization_id=org,
        agent_id=agent_id,
        provider="twilio",
        from_e164=person,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status="received",
        body=body,
        requested_at=at or datetime.now(timezone.utc),
    )


async def test_new_inbound_texts_only_for_this_thread(async_session):
    org, agent, call = await _seed_call(async_session)
    after = datetime.now(timezone.utc) - timedelta(seconds=1)
    async_session.add(_text(org, agent.id, "mine"))
    async_session.add(_text(org, agent.id, "other caller", person="+33600000000"))
    async_session.add(_text(org, agent.id, "too old", at=after - timedelta(minutes=1)))
    await async_session.commit()

    rows = await text_watch.new_inbound_texts(
        async_session, org, agent.id, PERSON, after
    )

    assert [r.body for r in rows] == ["mine"]


async def test_watcher_injects_a_new_text_once(async_session):
    org, agent, call = await _seed_call(async_session)
    fake = FakeSession()
    task = asyncio.create_task(
        text_watch.watch_incoming_texts(fake, call.id, poll_seconds=0.05)
    )
    await asyncio.sleep(0.1)
    async_session.add(_text(org, agent.id, "my address is 5 Rue X"))
    await async_session.commit()
    await asyncio.sleep(0.3)
    task.cancel()

    assert fake.inputs == [threads.TEXT_MARKER + "my address is 5 Rue X"]


async def test_long_text_is_cut_before_injection(async_session):
    org, agent, call = await _seed_call(async_session)
    fake = FakeSession()
    task = asyncio.create_task(
        text_watch.watch_incoming_texts(fake, call.id, poll_seconds=0.05)
    )
    await asyncio.sleep(0.1)
    async_session.add(_text(org, agent.id, "y" * 5000))
    await async_session.commit()
    await asyncio.sleep(0.3)
    task.cancel()

    assert len(fake.inputs[0]) == len(threads.TEXT_MARKER) + text_watch.MAX_INJECT_CHARS
