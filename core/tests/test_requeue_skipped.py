"""Texts skipped for a call go back to the text agent when the call ends."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import text_agent, threads
from hailhq.core.models import Agent, Call, PhoneNumber, Sms
from hailhq.core.reconcile import sweep_stale_calls
from sqlalchemy import update

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


def _now():
    return datetime.now(timezone.utc)


async def _seed(session, *, direction="outbound", ring_seconds=45, status="ringing"):
    org = uuid.uuid4()
    agent = Agent(
        organization_id=org,
        name="a",
        system_prompt="x",
        sms_enabled=True,
        status="live",
    )
    session.add(agent)
    await session.flush()
    number = PhoneNumber(
        organization_id=org,
        e164=ORG_NUMBER,
        country_code="US",
        number_type="local",
        provider="twilio",
        provisioning_state="active",
        sms_agent_id=agent.id,
    )
    session.add(number)
    await session.flush()
    inbound = direction == "inbound"
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id if inbound else None,
        from_number_id=None if inbound else number.id,
        from_e164=PERSON if inbound else ORG_NUMBER,
        to_e164=ORG_NUMBER if inbound else PERSON,
        direction=direction,
        status=status,
        provider="twilio",
        voice_config={},
        max_duration_seconds=60,
    )
    session.add(call)
    await session.commit()
    created = _now() - timedelta(seconds=ring_seconds)
    await session.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(created_at=created, requested_at=created, started_at=created)
    )
    await session.commit()
    await session.refresh(call)
    return org, agent, call


def _text(org, agent_id, body, *, at, state="skipped", reason="active_call", **kw):
    return Sms(
        organization_id=org,
        agent_id=agent_id,
        provider="twilio",
        from_e164=kw.pop("person", PERSON),
        to_e164=ORG_NUMBER,
        direction=kw.pop("direction", "inbound"),
        status="received",
        body=body,
        requested_at=at,
        agent_reply_state=state,
        metadata_={"skipped_reason": reason} if reason else {},
    )


async def test_ringing_window_text_is_revived_and_others_are_not(async_session):
    org, agent, call = await _seed(async_session)
    other_agent = Agent(organization_id=org, name="b", system_prompt="x")
    async_session.add(other_agent)
    await async_session.flush()
    created = call.created_at
    rows = {
        "ringing": _text(org, agent.id, "ringing", at=created + timedelta(seconds=10)),
        "overlap": _text(org, agent.id, "overlap", at=created - timedelta(seconds=10)),
        "before": _text(org, agent.id, "before", at=created - timedelta(minutes=5)),
        "caller": _text(org, agent.id, "caller", at=created, person="+33600000000"),
        "agent": _text(org, other_agent.id, "agent", at=created),
        "org": _text(uuid.uuid4(), agent.id, "org", at=created),
        "other": _text(org, agent.id, "other", at=created, reason="expired"),
        "none": _text(org, agent.id, "none", at=created, reason=None),
        "done": _text(org, agent.id, "done", at=created, state="done"),
        "outbound": _text(org, agent.id, "out", at=created, direction="outbound"),
    }
    async_session.add_all(rows.values())
    await async_session.commit()

    n = await threads.requeue_skipped_for_call(async_session, call)
    await async_session.commit()

    assert n == 2
    states = {}
    for key, r in rows.items():
        await async_session.refresh(r)
        states[key] = r.agent_reply_state
    assert states == {
        "ringing": "pending",
        "overlap": "pending",
        "before": "skipped",
        "caller": "skipped",
        "agent": "skipped",
        "org": "skipped",
        "other": "skipped",
        "none": "skipped",
        "done": "done",
        "outbound": "skipped",
    }
    revived = rows["ringing"]
    assert "skipped_reason" not in revived.metadata_
    assert "requeued_at" in revived.metadata_
    # A second pass finds nothing: a row is revived once.
    assert await threads.requeue_skipped_for_call(async_session, call) == 0


async def test_revived_text_older_than_the_age_limit_is_answerable(async_session):
    org, agent, call = await _seed(async_session, ring_seconds=10)
    old = _text(org, agent.id, "very old", at=_now() - timedelta(hours=2))
    # Created long before the text: inside the window of a call row that old.
    await async_session.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(created_at=_now() - timedelta(hours=3))
    )
    old.to_number_id = call.from_number_id
    async_session.add(old)
    await async_session.commit()
    await async_session.refresh(call)

    assert await threads.requeue_skipped_for_call(async_session, call) == 1
    await async_session.commit()
    await async_session.refresh(old)
    assert old.requested_at > _now() - timedelta(minutes=1)

    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None and claimed.sms.id == old.id
    await async_session.refresh(old)
    assert old.agent_reply_state == "processing"


async def test_inbound_call_uses_the_caller_in_from(async_session):
    org, agent, call = await _seed(
        async_session, direction="inbound", status="in_progress"
    )
    row = _text(org, agent.id, "hi", at=call.created_at + timedelta(seconds=5))
    async_session.add(row)
    await async_session.commit()
    assert await threads.requeue_skipped_for_call(async_session, call) == 1


async def test_withheld_caller_or_no_agent_revives_nothing(async_session):
    org, agent, call = await _seed(async_session)
    row = _text(org, agent.id, "hi", at=call.created_at + timedelta(seconds=5))
    async_session.add(row)
    await async_session.commit()
    call.to_e164 = "anonymous"
    assert await threads.requeue_skipped_for_call(async_session, call) == 0
    call.to_e164 = PERSON
    call.agent_id = None
    assert await threads.requeue_skipped_for_call(async_session, call) == 0
    await async_session.rollback()


async def test_stale_sweep_revives_skipped_texts(async_session):
    org, agent, call = await _seed(async_session, ring_seconds=3600)
    row = _text(
        org, agent.id, "while ringing", at=call.created_at + timedelta(seconds=5)
    )
    async_session.add(row)
    await async_session.commit()

    closed = await sweep_stale_calls(async_session, grace_seconds=0)
    await async_session.commit()

    assert closed == [call.id]
    await async_session.refresh(row)
    assert row.agent_reply_state == "pending"


async def test_stale_sweep_without_agent_still_closes(async_session):
    _org, _agent, call = await _seed(async_session, ring_seconds=3600)
    await async_session.execute(
        update(Call).where(Call.id == call.id).values(agent_id=None)
    )
    await async_session.commit()
    assert await sweep_stale_calls(async_session, grace_seconds=0) == [call.id]


def test_is_e164_keeps_the_seven_digit_minimum():
    assert threads.is_e164("+33612345678")
    assert threads.is_e164("+1234567")
    assert not threads.is_e164("+123456")  # E164 allows it; not a real caller
    for bad in ("", None, "anonymous", "0612345678", "+0123456789", "+" + "1" * 16):
        assert not threads.is_e164(bad)
