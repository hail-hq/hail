"""Threads: one history per (org, agent, caller) over texts and call turns."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import threads
from hailhq.core.models import Agent, Call, CallEvent, PhoneNumber, Sms

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"
OTHER = "+33699999999"
NOW = datetime.now(timezone.utc)


async def _agent(session, org, name="a"):
    agent = Agent(organization_id=org, name=name, system_prompt="x")
    session.add(agent)
    await session.flush()
    return agent


def _sms(org, agent_id, *, inbound, person=PERSON, body="hi", at=NOW):
    return Sms(
        organization_id=org,
        agent_id=agent_id,
        provider="twilio",
        from_e164=person if inbound else ORG_NUMBER,
        to_e164=ORG_NUMBER if inbound else person,
        direction="inbound" if inbound else "outbound",
        status="received" if inbound else "sent",
        body=body,
        requested_at=at,
    )


async def _call(session, org, agent_id, *, person=PERSON, turns=(), status="completed"):
    number = PhoneNumber(
        organization_id=org,
        e164=f"+1415{uuid.uuid4().int % 10**7:07d}",
        country_code="US",
        number_type="local",
        provider="twilio",
        provisioning_state="active",
    )
    session.add(number)
    await session.flush()
    call = Call(
        organization_id=org,
        agent_id=agent_id,
        to_number_id=number.id,
        end_reason="normal_hangup" if status == "completed" else None,
        from_e164=person,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status=status,
        provider="twilio",
        voice_config={},
    )
    session.add(call)
    await session.flush()
    for i, (role, text) in enumerate(turns):
        session.add(
            CallEvent(
                call_id=call.id,
                kind="user_turn" if role == "user" else "agent_turn",
                payload={"role": role, "text": text},
                occurred_at=NOW + timedelta(seconds=i),
            )
        )
    await session.flush()
    return call


async def test_merges_texts_and_call_turns_in_time_order(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(
        _sms(org, agent.id, inbound=True, body="first", at=NOW - timedelta(minutes=5))
    )
    await _call(
        async_session,
        org,
        agent.id,
        turns=[("user", "hello"), ("assistant", "hi there")],
    )
    async_session.add(
        _sms(org, agent.id, inbound=False, body="last", at=NOW + timedelta(minutes=5))
    )
    await async_session.commit()

    items = await threads.thread_items(async_session, org, agent.id, PERSON)

    assert [(i.kind, i.text) for i in items] == [
        ("text_in", "first"),
        ("call_caller", "hello"),
        ("call_agent", "hi there"),
        ("text_out", "last"),
    ]


async def test_other_caller_and_other_agent_never_appear(async_session):
    org = uuid.uuid4()
    a = await _agent(async_session, org, "a")
    b = await _agent(async_session, org, "b")
    async_session.add(_sms(org, a.id, inbound=True, body="mine"))
    async_session.add(_sms(org, a.id, inbound=True, person=OTHER, body="other caller"))
    async_session.add(_sms(org, b.id, inbound=True, body="other agent"))
    await _call(async_session, org, a.id, person=OTHER, turns=[("user", "secret call")])
    await _call(async_session, org, b.id, turns=[("user", "other agent call")])
    await async_session.commit()

    items = await threads.thread_items(async_session, org, a.id, PERSON)

    assert [i.text for i in items] == ["mine"]


async def test_other_org_never_appears(async_session):
    org, other_org = uuid.uuid4(), uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(_sms(other_org, agent.id, inbound=True, body="other org"))
    await async_session.commit()

    assert await threads.thread_items(async_session, org, agent.id, PERSON) == []


async def test_window_and_limit(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(
        _sms(org, agent.id, inbound=True, body="old", at=NOW - timedelta(days=8))
    )
    for n in range(35):
        async_session.add(
            _sms(
                org,
                agent.id,
                inbound=True,
                body=f"m{n}",
                at=NOW - timedelta(minutes=40 - n),
            )
        )
    await async_session.commit()

    items = await threads.thread_items(async_session, org, agent.id, PERSON)

    assert len(items) == threads.THREAD_LIMIT
    assert items[-1].text == "m34"
    assert all(i.text != "old" for i in items)


async def test_before_cursor_returns_older_items_only(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    for n in range(5):
        async_session.add(
            _sms(
                org,
                agent.id,
                inbound=True,
                body=f"m{n}",
                at=NOW - timedelta(minutes=10 - n),
            )
        )
    await async_session.commit()
    items = await threads.thread_items(async_session, org, agent.id, PERSON, limit=2)
    assert [i.text for i in items] == ["m3", "m4"]

    older = await threads.thread_items(
        async_session, org, agent.id, PERSON, limit=2, before=items[0].id
    )

    assert [i.text for i in older] == ["m1", "m2"]


async def test_injected_text_turn_is_not_duplicated(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    async_session.add(_sms(org, agent.id, inbound=True, body="my address is 5 Rue X"))
    await _call(
        async_session,
        org,
        agent.id,
        turns=[
            ("user", threads.TEXT_MARKER + "my address is 5 Rue X"),
            ("user", "did you get it"),
        ],
    )
    await async_session.commit()

    items = await threads.thread_items(async_session, org, agent.id, PERSON)

    assert [i.text for i in items] == ["my address is 5 Rue X", "did you get it"]


async def test_thread_item_is_scoped(async_session):
    org = uuid.uuid4()
    a = await _agent(async_session, org, "a")
    b = await _agent(async_session, org, "b")
    row = _sms(org, b.id, inbound=True, body="private")
    async_session.add(row)
    await async_session.commit()

    assert (
        await threads.thread_item(async_session, org, a.id, PERSON, f"sms:{row.id}")
        is None
    )
    found = await threads.thread_item(async_session, org, b.id, PERSON, f"sms:{row.id}")
    assert found.text == "private"
    assert (
        await threads.thread_item(async_session, org, b.id, PERSON, "garbage") is None
    )


async def test_call_thread_key_and_active_call(async_session):
    org = uuid.uuid4()
    agent = await _agent(async_session, org)
    live = await _call(async_session, org, agent.id, status="in_progress")
    done = await _call(async_session, org, agent.id, person=OTHER, status="completed")
    await async_session.commit()

    assert await threads.call_thread_key(async_session, live.id) == (
        org,
        agent.id,
        PERSON,
    )
    active = await threads.active_call_for_thread(async_session, org, agent.id, PERSON)
    assert active.id == live.id
    assert (
        await threads.active_call_for_thread(async_session, org, agent.id, OTHER)
        is None
    )
    assert done.id != live.id


def test_render_cuts_long_text_and_labels_channels():
    long = "x" * 600
    items = [
        threads.ThreadItem("sms:1", NOW, "text_in", long),
        threads.ThreadItem("event:2", NOW, "call_caller", "hello"),
        threads.ThreadItem("event:3", NOW, "call_agent", "hi"),
        threads.ThreadItem("sms:4", NOW, "text_out", "sent"),
    ]

    out = threads.render_thread(items)

    assert "x" * 500 + "..." in out and "x" * 501 not in out
    assert "thread_history" in out  # the cut note names the tool
    assert "caller: hello" in out and "you: hi" in out
    assert "text from caller" in out and "text from you" in out
    assert "x" * 600 in threads.render_thread(items, cut=None)
    assert threads.render_thread([]) == ""
