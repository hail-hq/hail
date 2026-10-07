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
POLL = 0.02


class FakeSession:
    def __init__(self, raises: list[Exception] | None = None) -> None:
        self.inputs: list[str] = []
        self.raises = list(raises or [])

    def generate_reply(self, **kwargs) -> None:
        if self.raises:
            raise self.raises.pop(0)
        self.inputs.append(kwargs["user_input"])


async def wait_for(cond, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


async def _stop(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _seed_call(async_session, *, caller=PERSON):
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
        from_e164=caller,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status="in_progress",
        provider="twilio",
        voice_config={},
    )
    async_session.add(call)
    await async_session.commit()
    return org, agent, call


def _text(
    org,
    agent_id,
    body,
    *,
    person=PERSON,
    at,
    inbound=True,
    id=None,
    state="skipped",
    reason="active_call",
):
    return Sms(
        id=id or uuid.uuid4(),
        organization_id=org,
        agent_id=agent_id,
        provider="twilio",
        from_e164=person if inbound else ORG_NUMBER,
        to_e164=ORG_NUMBER if inbound else person,
        direction="inbound" if inbound else "outbound",
        status="received" if inbound else "sent",
        body=body,
        requested_at=at,
        agent_reply_state=state,
        metadata_=({"skipped_reason": reason} if reason is not None else {}),
    )


def _start(fake, call, since, **kw):
    return asyncio.create_task(
        text_watch.watch_incoming_texts(
            fake, call.id, since=since, poll_seconds=POLL, **kw
        )
    )


def _now():
    return datetime.now(timezone.utc)


async def test_text_injected_exactly_once_across_polls(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    fake = FakeSession()
    task = _start(fake, call, since)
    async_session.add(
        _text(org, agent.id, "my address is 5 Rue X", at=since + timedelta(seconds=1))
    )
    await async_session.commit()
    await wait_for(lambda: len(fake.inputs) == 1)
    await asyncio.sleep(POLL * 8)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "my address is 5 Rue X"]


async def test_long_text_is_cut_before_injection(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    fake = FakeSession()
    task = _start(fake, call, since)
    async_session.add(_text(org, agent.id, "y" * 5000, at=since + timedelta(seconds=1)))
    await async_session.commit()
    await wait_for(lambda: len(fake.inputs) == 1)
    await _stop(task)
    assert fake.inputs[0] == threads.TEXT_MARKER + "y" * 1000


async def test_overlap_window_delivers_slightly_old_text_only(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    fake = FakeSession()
    async_session.add(_text(org, agent.id, "recent", at=since - timedelta(seconds=10)))
    async_session.add(_text(org, agent.id, "stale", at=since - timedelta(minutes=5)))
    await async_session.commit()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) >= 1)
    await asyncio.sleep(POLL * 8)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "recent"]


async def test_two_texts_in_requested_at_then_id_order(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    fake = FakeSession()
    t = since + timedelta(seconds=1)
    low, high = sorted([uuid.uuid4(), uuid.uuid4()])
    # Inserted out of order; same timestamp for the last two.
    async_session.add(_text(org, agent.id, "third", at=t, id=high))
    async_session.add(_text(org, agent.id, "second", at=t, id=low))
    async_session.add(_text(org, agent.id, "first", at=t - timedelta(seconds=1)))
    await async_session.commit()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) == 3)
    await _stop(task)
    assert [i.removeprefix(threads.TEXT_MARKER) for i in fake.inputs] == [
        "first",
        "second",
        "third",
    ]


async def test_other_org_agent_caller_and_outbound_are_excluded(async_session):
    org, agent, call = await _seed_call(async_session)
    other_agent = Agent(organization_id=org, name="b", system_prompt="x")
    async_session.add(other_agent)
    await async_session.flush()
    since = _now()
    at = since + timedelta(seconds=1)
    async_session.add(_text(org, agent.id, "mine", at=at))
    async_session.add(_text(uuid.uuid4(), agent.id, "other org", at=at))
    async_session.add(_text(org, other_agent.id, "other agent", at=at))
    async_session.add(
        _text(org, agent.id, "other caller", person="+33600000000", at=at)
    )
    async_session.add(_text(org, agent.id, "outbound", at=at, inbound=False))
    await async_session.commit()
    fake = FakeSession()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) >= 1)
    await asyncio.sleep(POLL * 8)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "mine"]


async def test_failed_poll_continues(async_session, monkeypatch):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    real = text_watch.new_inbound_texts
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("db blip")
        return await real(*a, **kw)

    monkeypatch.setattr(text_watch, "new_inbound_texts", flaky)
    async_session.add(_text(org, agent.id, "hi", at=since + timedelta(seconds=1)))
    await async_session.commit()
    fake = FakeSession()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) == 1)
    await _stop(task)
    assert calls["n"] >= 2


async def test_generate_reply_failure_is_retried_not_marked_delivered(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    async_session.add(_text(org, agent.id, "retry me", at=since + timedelta(seconds=1)))
    await async_session.commit()
    fake = FakeSession(raises=[ValueError("boom")])
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) == 1)
    await asyncio.sleep(POLL * 8)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "retry me"]


async def test_generate_reply_failing_three_times_is_given_up(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    bad = _text(org, agent.id, "bad", at=since + timedelta(seconds=1))
    good = _text(org, agent.id, "good", at=since + timedelta(seconds=2))
    async_session.add_all([bad, good])
    await async_session.commit()

    class Picky(FakeSession):
        attempts = 0

        def generate_reply(self, **kwargs) -> None:
            if kwargs["user_input"].endswith("bad"):
                Picky.attempts += 1
                raise ValueError("boom")
            super().generate_reply(**kwargs)

    fake = Picky()
    delivered: set[uuid.UUID] = set()
    task = _start(fake, call, since, delivered=delivered)
    await wait_for(lambda: len(fake.inputs) == 1)
    await asyncio.sleep(POLL * 10)
    await _stop(task)
    assert Picky.attempts == 3
    assert fake.inputs == [threads.TEXT_MARKER + "good"]
    assert delivered == {bad.id, good.id}


async def test_only_skipped_texts_are_injected(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    at = since + timedelta(seconds=1)
    async_session.add(_text(org, agent.id, "skipped", at=at))
    for state in ("pending", "processing", "done", "failed", None):
        async_session.add(_text(org, agent.id, state or "none", at=at, state=state))
    await async_session.commit()
    fake = FakeSession()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) >= 1)
    await asyncio.sleep(POLL * 8)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "skipped"]


async def test_rows_skipped_for_other_reasons_are_never_injected_or_requeued(
    async_session,
):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    at = since + timedelta(seconds=1)
    unmarked = _text(org, agent.id, "unmarked", at=at, reason=None)
    expired = _text(org, agent.id, "expired", at=at, reason="expired")
    mine = _text(org, agent.id, "mine", at=at)
    async_session.add_all([unmarked, expired, mine])
    await async_session.commit()

    fake = FakeSession()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) >= 1)
    await asyncio.sleep(POLL * 8)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "mine"]

    n = await text_watch.requeue_undelivered((org, agent.id, PERSON), since, set())
    assert n == 1
    for r, state in ((unmarked, "skipped"), (expired, "skipped"), (mine, "pending")):
        await async_session.refresh(r)
        assert r.agent_reply_state == state


async def test_requeue_undelivered_only_touches_this_threads_undelivered(
    async_session,
):
    org, agent, _call = await _seed_call(async_session)
    other_agent = Agent(organization_id=org, name="b", system_prompt="x")
    async_session.add(other_agent)
    await async_session.flush()
    since = _now()
    at = since + timedelta(seconds=1)
    delivered = _text(org, agent.id, "delivered", at=at)
    missed = _text(org, agent.id, "missed", at=at)
    slightly_early = _text(org, agent.id, "early", at=since - timedelta(seconds=10))
    too_old = _text(org, agent.id, "old", at=since - timedelta(minutes=5))
    other_caller = _text(org, agent.id, "caller", person="+33600000000", at=at)
    other_ag = _text(org, other_agent.id, "agent", at=at)
    other_org = _text(uuid.uuid4(), agent.id, "org", at=at)
    outbound = _text(org, agent.id, "out", at=at, inbound=False)
    done = _text(org, agent.id, "done", at=at, state="done")  # answered: stays
    rows = [
        delivered, missed, slightly_early, too_old, other_caller, other_ag,
        other_org, outbound, done,
    ]  # fmt: skip
    async_session.add_all(rows)
    await async_session.commit()

    n = await text_watch.requeue_undelivered(
        (org, agent.id, PERSON), since, {delivered.id}
    )

    assert n == 2
    states = {}
    for r in rows:
        await async_session.refresh(r)
        states[r.body] = r.agent_reply_state
    assert states == {
        "delivered": "skipped",
        "missed": "pending",
        "early": "pending",
        "old": "skipped",
        "caller": "skipped",
        "agent": "skipped",
        "org": "skipped",
        "out": "skipped",
        "done": "done",
    }


async def test_closed_session_stops_the_loop(async_session):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    async_session.add(_text(org, agent.id, "late", at=since + timedelta(seconds=1)))
    await async_session.commit()
    fake = FakeSession(raises=[RuntimeError("session closed")])
    task = _start(fake, call, since)
    await wait_for(task.done)
    assert task.exception() is None
    assert fake.inputs == []


async def test_withheld_caller_returns_immediately(async_session):
    _org, _agent, call = await _seed_call(async_session, caller="anonymous")
    fake = FakeSession()
    task = _start(fake, call, _now())
    await wait_for(task.done)
    assert task.exception() is None
    assert fake.inputs == []


async def test_db_runtime_error_does_not_stop_the_watcher(async_session, monkeypatch):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    real = text_watch.new_inbound_texts
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("pool blip")
        return await real(*a, **kw)

    monkeypatch.setattr(text_watch, "new_inbound_texts", flaky)
    async_session.add(
        _text(org, agent.id, "still here", at=since + timedelta(seconds=1))
    )
    await async_session.commit()
    fake = FakeSession()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) == 1)
    await _stop(task)
    assert fake.inputs == [threads.TEXT_MARKER + "still here"]


async def test_transient_key_lookup_failure_is_retried(async_session, monkeypatch):
    org, agent, call = await _seed_call(async_session)
    since = _now()
    real = threads.call_thread_key
    calls = {"n": 0}

    async def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("db blip")
        return await real(*a, **kw)

    monkeypatch.setattr(threads, "call_thread_key", flaky)
    async_session.add(_text(org, agent.id, "hello", at=since + timedelta(seconds=1)))
    await async_session.commit()
    fake = FakeSession()
    task = _start(fake, call, since)
    await wait_for(lambda: len(fake.inputs) == 1)
    await _stop(task)
    assert calls["n"] == 2
