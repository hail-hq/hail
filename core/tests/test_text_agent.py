"""Text agent: queueing on ingest, claiming, history, and the loop breaker."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import text_agent, threads
from hailhq.core.models import Agent, Call, CallEvent, PhoneNumber, Sms
from hailhq.core.prompts import THREAD_TOOL_HINT_TEXT
from hailhq.core.sms_ingest import ingest_inbound_sms
from sqlalchemy import select

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


async def _seed(session, *, sms_enabled=True, status="live", route=True):
    org = uuid.uuid4()
    agent = Agent(
        organization_id=org,
        name="Front desk",
        system_prompt="Book appointments.",
        sms_enabled=sms_enabled,
        status=status,
    )
    session.add(agent)
    await session.flush()
    number = PhoneNumber(
        organization_id=org,
        e164=ORG_NUMBER,
        country_code="US",
        number_type="local",
        provisioning_state="active",
        provider_resource_id="PN",
        sms_agent_id=agent.id if route else None,
    )
    session.add(number)
    await session.commit()
    return org, agent, number


async def _ingest(session, body: str, sid: str, opt_out_type=None):
    return await ingest_inbound_sms(
        session,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        body=body,
        provider_message_sid=sid,
        opt_out_type=opt_out_type,
        carrier="twilio",
    )


async def test_plain_text_on_routed_number_is_queued(async_session) -> None:
    await _seed(async_session)
    result = await _ingest(async_session, "Can I book for Tuesday?", "SM1")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state == "pending"


async def test_stop_is_never_handed_to_the_agent(async_session) -> None:
    await _seed(async_session)
    result = await _ingest(async_session, "STOP", "SM2", opt_out_type="STOP")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state is None


async def test_yes_from_a_person_not_opted_out_goes_to_the_agent(
    async_session,
) -> None:
    await _seed(async_session)
    for sid, word, kind in (("SM_y1", "YES", None), ("SM_y2", "start", "START")):
        result = await _ingest(async_session, word, sid, opt_out_type=kind)
        row = await async_session.get(Sms, result.sms_id)
        assert row.agent_reply_state == "pending", word


async def test_yes_from_an_opted_out_person_opts_them_back_in(async_session) -> None:
    from hailhq.core.compliance_gate import add_suppression, is_suppressed

    org, _agent, _number = await _seed(async_session)
    await add_suppression(
        async_session,
        organization_id=org,
        recipient=PERSON,
        channel="sms",
        reason="prior stop",
        source="stop_keyword",
    )
    await async_session.commit()
    result = await _ingest(async_session, "YES", "SM_y3")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state is None
    assert not await is_suppressed(async_session, org, PERSON, "sms")


async def test_help_is_never_handed_to_the_agent(async_session) -> None:
    await _seed(async_session)
    result = await _ingest(async_session, "HELP", "SM_h1")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state is None


async def test_unrouted_paused_or_sms_disabled_agent_is_not_queued(
    async_session,
) -> None:
    for kwargs in ({"route": False}, {"status": "paused"}, {"sms_enabled": False}):
        session = async_session
        await _seed(session, **kwargs)
        result = await _ingest(session, "hi", f"SM-{kwargs}")
        row = await session.get(Sms, result.sms_id)
        assert row.agent_reply_state is None, kwargs
        # Each loop seeds a new org/number with the same e164; release the
        # live-uniqueness slot for the next iteration.
        number = (
            await session.execute(
                select(PhoneNumber).where(
                    PhoneNumber.e164 == ORG_NUMBER,
                    PhoneNumber.provisioning_state == "active",
                )
            )
        ).scalar_one()
        number.provisioning_state = "released"
        await session.commit()


async def test_claim_history_and_chat_shape(async_session) -> None:
    org, agent, number = await _seed(async_session)
    base = datetime.now(timezone.utc) - timedelta(minutes=30)
    # 22 earlier inbound messages plus one old agent reply.
    for i in range(22):
        async_session.add(
            Sms(
                organization_id=org,
                to_number_id=number.id,
                agent_id=agent.id,
                from_e164=PERSON,
                to_e164=ORG_NUMBER,
                direction="inbound",
                status="received",
                body=f"msg {i}",
                requested_at=base + timedelta(seconds=i),
            )
        )
    async_session.add(
        Sms(
            organization_id=org,
            from_number_id=number.id,
            agent_id=agent.id,
            from_e164=ORG_NUMBER,
            to_e164=PERSON,
            direction="outbound",
            status="sent",
            body="Sure.",
            metadata_={"reply_to_sms_id": str(uuid.uuid4())},
            requested_at=base + timedelta(seconds=22),
        )
    )
    # A voice send_sms row (no reply_to_sms_id) is history but not a reply.
    async_session.add(
        Sms(
            organization_id=org,
            from_number_id=number.id,
            agent_id=agent.id,
            from_e164=ORG_NUMBER,
            to_e164=PERSON,
            direction="outbound",
            status="sent",
            body="Voice sent.",
            metadata_={"call_id": str(uuid.uuid4())},
            requested_at=base + timedelta(seconds=23),
        )
    )
    await async_session.commit()
    result = await _ingest(async_session, "Tuesday then?", "SM3")

    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None
    assert claimed.sms.id == result.sms_id
    assert claimed.agent.id == agent.id

    history = await text_agent.thread_history_for_reply(
        async_session, claimed.sms, agent
    )
    # The last CHAT_LIMIT texts, the new one included.
    assert len(history) == text_agent.CHAT_LIMIT == 20
    assert history[-1].text == "Tuesday then?"
    assert history[-2].text == "Voice sent."
    assert history[-3].text == "Sure."
    assert history[0].text == "msg 5"  # oldest first, in order

    messages = text_agent.build_chat_messages(agent, history)
    assert messages[0]["role"] == "system"
    assert "Book appointments." in messages[0]["content"]
    assert text_agent.TEXT_PREAMBLE in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Tuesday then?"}
    assert messages[-2] == {"role": "assistant", "content": "Voice sent."}
    assert messages[-3] == {"role": "assistant", "content": "Sure."}

    assert await text_agent.replies_in_thread(async_session, claimed.sms) == 1

    await text_agent.finish_reply(async_session, claimed.sms, "done")
    assert await text_agent.claim_pending_reply(async_session) is None


async def test_claim_skips_when_routing_changed(async_session) -> None:
    _org, agent, _number = await _seed(async_session)
    result = await _ingest(async_session, "hi", "SM4")
    agent.status = "paused"
    await async_session.commit()
    assert await text_agent.claim_pending_reply(async_session) is None
    row = await async_session.get(Sms, result.sms_id)
    await async_session.refresh(row)
    assert row.agent_reply_state == "skipped"
    assert "skipped_reason" not in row.metadata_  # not an active-call skip


async def test_claim_moves_past_dropped_texts_to_the_next_row(async_session) -> None:
    """A text whose agent went away is skipped and the same call takes the next
    pending row, so a backlog of dropped texts does not cost a poll each."""
    org, _agent, _number = await _seed(async_session)
    first = await _ingest(async_session, "one", "SM10")
    second = await _ingest(async_session, "two", "SM11")
    # The first row now points at a number with no text agent; the second at
    # the live one.
    other = PhoneNumber(
        organization_id=org,
        e164="+14155550199",
        country_code="US",
        number_type="local",
        provisioning_state="active",
        provider_resource_id="PN2",
    )
    async_session.add(other)
    await async_session.flush()
    row1 = await async_session.get(Sms, first.sms_id)
    row1.to_number_id = other.id
    row1.requested_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    await async_session.commit()

    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None
    assert claimed.sms.id == second.sms_id
    await async_session.refresh(row1)
    assert row1.agent_reply_state == "skipped"


async def test_claim_marks_processing_and_commits(
    async_session, session_factory
) -> None:
    """The claim holds no row lock: another session can read and update it."""
    await _seed(async_session)
    result = await _ingest(async_session, "hi", "SM20")
    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None and claimed.attempt == 1
    async with session_factory() as other:
        row = await other.get(Sms, result.sms_id)
        assert row.agent_reply_state == "processing"
        assert row.agent_reply_attempts == 1
        assert row.agent_reply_available_at > datetime.now(timezone.utc)
        # Not lockable-blocking: NOWAIT would raise if the claim kept the lock.
        await other.execute(
            select(Sms).where(Sms.id == result.sms_id).with_for_update(nowait=True)
        )
    # A live lease is not claimed twice.
    async with session_factory() as other:
        assert await text_agent.claim_pending_reply(other) is None


async def test_two_workers_claim_different_texts(
    async_session, session_factory
) -> None:
    await _seed(async_session)
    await _ingest(async_session, "one", "SM21")
    await _ingest(async_session, "two", "SM22")
    await async_session.commit()
    async with session_factory() as a, session_factory() as b:
        first = await text_agent.claim_pending_reply(a)
        second = await text_agent.claim_pending_reply(b)
        assert first is not None and second is not None
        assert first.sms.id != second.sms.id
        assert await text_agent.claim_pending_reply(a) is None


async def test_expired_lease_is_reclaimed_then_fails_after_max_attempts(
    async_session,
) -> None:
    await _seed(async_session)
    result = await _ingest(async_session, "hi", "SM23")
    row = await async_session.get(Sms, result.sms_id)
    for attempt in range(1, text_agent.MAX_ATTEMPTS + 1):
        claimed = await text_agent.claim_pending_reply(async_session)
        assert claimed is not None and claimed.attempt == attempt
        # The worker dies: its lease runs out.
        row.agent_reply_available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await async_session.commit()
    assert await text_agent.claim_pending_reply(async_session) is None
    await async_session.refresh(row)
    assert row.agent_reply_state == "failed"


async def test_retry_waits_for_backoff_then_gives_up(async_session) -> None:
    await _seed(async_session)
    result = await _ingest(async_session, "hi", "SM24")
    row = await async_session.get(Sms, result.sms_id)

    claimed = await text_agent.claim_pending_reply(async_session)
    assert await text_agent.retry_reply(async_session, claimed) == "pending"
    await async_session.refresh(row)
    assert row.agent_reply_state == "pending"
    assert row.agent_reply_available_at > datetime.now(timezone.utc)
    assert await text_agent.claim_pending_reply(async_session) is None  # backoff

    row.agent_reply_available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await async_session.commit()
    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None and claimed.attempt == 2

    row.agent_reply_attempts = text_agent.MAX_ATTEMPTS
    await async_session.commit()
    claimed = text_agent.ClaimedReply(
        sms=claimed.sms, agent=claimed.agent, number=claimed.number, attempt=3
    )
    assert await text_agent.retry_reply(async_session, claimed) == "failed"
    await async_session.refresh(row)
    assert row.agent_reply_state == "failed"


async def test_stale_claim_does_not_overwrite_a_reclaimed_row(async_session) -> None:
    await _seed(async_session)
    result = await _ingest(async_session, "hi", "SM25")
    row = await async_session.get(Sms, result.sms_id)
    old = await text_agent.claim_pending_reply(async_session)
    row.agent_reply_available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await async_session.commit()
    new = await text_agent.claim_pending_reply(async_session)
    assert new is not None and new.attempt == 2
    await text_agent.finish_reply(async_session, old.sms, "failed", attempt=old.attempt)
    assert await text_agent.retry_reply(async_session, old) in ("pending", None)
    await async_session.refresh(row)
    # The old worker's retry targeted attempt 1, which no longer owns the row.
    assert row.agent_reply_state == "processing"
    await text_agent.finish_reply(async_session, new.sms, "done", attempt=new.attempt)
    await async_session.refresh(row)
    assert row.agent_reply_state == "done"


async def test_old_pending_text_is_skipped_not_answered(
    async_session, monkeypatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_text_reply_max_age_seconds", 60)
    await _seed(async_session)
    old = await _ingest(async_session, "old", "SM26")
    fresh = await _ingest(async_session, "fresh", "SM27")
    row = await async_session.get(Sms, old.sms_id)
    row.requested_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    await async_session.commit()

    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None and claimed.sms.id == fresh.sms_id
    await async_session.refresh(row)
    assert row.agent_reply_state == "skipped"
    assert "skipped_reason" not in row.metadata_  # expiry is not a call skip


async def test_inbound_text_carries_the_numbers_agent(async_session) -> None:
    _, agent, _ = await _seed(async_session)
    result = await _ingest(async_session, "Hello", "SM9")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_id == agent.id


async def test_inbound_text_to_paused_agent_still_belongs_to_it(async_session) -> None:
    _, agent, _ = await _seed(async_session, status="paused")
    result = await _ingest(async_session, "Hello", "SM10")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_id == agent.id
    assert row.agent_reply_state is None


async def test_inbound_text_on_unrouted_number_has_no_agent(async_session) -> None:
    await _seed(async_session, route=False)
    result = await _ingest(async_session, "Hello", "SM11")
    assert (await async_session.get(Sms, result.sms_id)).agent_id is None


async def _call(session, org, agent, number, *, status, turns=()):
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status=status,
        end_reason="normal_hangup" if status == "completed" else None,
        provider="twilio",
        voice_config={},
    )
    session.add(call)
    await session.flush()
    for role, text in turns:
        session.add(
            CallEvent(
                call_id=call.id,
                kind="user_turn" if role == "user" else "agent_turn",
                payload={"role": role, "text": text},
            )
        )
    await session.commit()
    return call


async def test_reply_chat_has_texts_only_no_call_lines(async_session) -> None:
    org, agent, number = await _seed(async_session)
    async_session.add(
        Sms(
            organization_id=org,
            from_number_id=number.id,
            agent_id=agent.id,
            from_e164=ORG_NUMBER,
            to_e164=PERSON,
            direction="outbound",
            status="sent",
            body="See you Tuesday.",
            requested_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        )
    )
    await async_session.commit()
    await _call(
        async_session,
        org,
        agent,
        number,
        status="completed",
        turns=[("user", "I want Tuesday"), ("assistant", "I don't have that.")],
    )
    result = await _ingest(async_session, "Did you book it?", "SM20")
    sms = await async_session.get(Sms, result.sms_id)

    history = await text_agent.thread_history_for_reply(async_session, sms, agent)
    messages = text_agent.build_chat_messages(agent, history)

    assert {i.kind for i in history} <= {"text_in", "text_out"}
    assert messages[1:] == [
        {"role": "assistant", "content": "See you Tuesday."},
        {"role": "user", "content": "Did you book it?"},
    ]
    joined = " ".join(m["content"] for m in messages)
    assert "I want Tuesday" not in joined and "(on a call)" not in joined
    assert "I don't have that." not in joined


async def test_reply_chat_keeps_only_the_last_24_hours(async_session) -> None:
    org, agent, number = await _seed(async_session)
    for hours, body in ((30, "day old"), (2, "two hours")):
        async_session.add(
            Sms(
                organization_id=org,
                to_number_id=number.id,
                agent_id=agent.id,
                from_e164=PERSON,
                to_e164=ORG_NUMBER,
                direction="inbound",
                status="received",
                body=body,
                requested_at=datetime.now(timezone.utc) - timedelta(hours=hours),
            )
        )
    await async_session.commit()
    result = await _ingest(async_session, "and now?", "SM24")
    sms = await async_session.get(Sms, result.sms_id)

    history = await text_agent.thread_history_for_reply(async_session, sms, agent)

    assert [i.text for i in history] == ["two hours", "and now?"]


async def test_system_prompt_names_the_thread_tool(async_session) -> None:
    agent = Agent(organization_id=uuid.uuid4(), name="A", system_prompt="Be nice.")
    messages = text_agent.build_chat_messages(agent, [])
    assert THREAD_TOOL_HINT_TEXT in messages[0]["content"]


async def test_text_during_an_active_call_is_not_queued(async_session) -> None:
    org, agent, number = await _seed(async_session)
    await _call(async_session, org, agent, number, status="in_progress")
    result = await _ingest(async_session, "my address is 5 Rue X", "SM21")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state == "skipped"
    assert row.metadata_ == {"skipped_reason": "active_call"}
    assert row.agent_id == agent.id


async def test_text_after_a_finished_call_is_queued(async_session) -> None:
    org, agent, number = await _seed(async_session)
    await _call(async_session, org, agent, number, status="completed")
    result = await _ingest(async_session, "hello again", "SM22")
    assert (await async_session.get(Sms, result.sms_id)).agent_reply_state == "pending"


async def test_current_text_is_last_even_on_a_timestamp_tie(async_session) -> None:
    org, agent, number = await _seed(async_session)
    at = datetime.now(timezone.utc) - timedelta(minutes=1)
    rows = []
    for i in range(2):
        row = Sms(
            organization_id=org,
            to_number_id=number.id,
            agent_id=agent.id,
            from_e164=PERSON,
            to_e164=ORG_NUMBER,
            direction="inbound",
            status="received",
            body=f"tie {i}",
            requested_at=at,
        )
        async_session.add(row)
        rows.append(row)
    await async_session.commit()
    for row in rows:  # whichever sorts first, the current one ends up last
        history = await text_agent.thread_history_for_reply(async_session, row, agent)
        assert [h.text for h in history].count(row.body) == 1
        assert history[-1].text == row.body
        assert len(history) == 2


async def test_active_call_of_another_caller_does_not_skip(async_session) -> None:
    org, agent, number = await _seed(async_session)
    call = await _call(async_session, org, agent, number, status="in_progress")
    call.from_e164 = "+33699999999"
    await async_session.commit()
    result = await _ingest(async_session, "hi", "SM30")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state == "pending"


async def test_active_call_of_another_agent_does_not_skip(async_session) -> None:
    org, _agent, number = await _seed(async_session)
    other = Agent(organization_id=org, name="Other", system_prompt="x")
    async_session.add(other)
    await async_session.flush()
    await _call(async_session, org, other, number, status="in_progress")
    result = await _ingest(async_session, "hi", "SM31")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state == "pending"


async def test_text_during_a_ringing_call_is_not_queued(async_session) -> None:
    org, agent, number = await _seed(async_session)
    await _call(async_session, org, agent, number, status="ringing")
    result = await _ingest(async_session, "hi", "SM32")
    row = await async_session.get(Sms, result.sms_id)
    assert row.agent_reply_state == "skipped"
    assert row.metadata_["skipped_reason"] == "active_call"


def test_chat_messages_map_texts_and_drop_call_items() -> None:
    from hailhq.core.threads import ThreadItem

    now = datetime.now(timezone.utc)
    agent = Agent(organization_id=uuid.uuid4(), name="A", system_prompt="Be nice.")
    history = [
        ThreadItem(id="event:1", at=now, kind="call_agent", text="Hello, Hail."),
        ThreadItem(id="event:2", at=now, kind="call_caller", text="Hi."),
        ThreadItem(id="sms:2", at=now, kind="text_out", text="Sent."),
        ThreadItem(id="sms:3", at=now, kind="text_in", text="Thanks"),
    ]
    messages = text_agent.build_chat_messages(agent, history)
    assert messages[1:] == [
        {"role": "assistant", "content": "Sent."},
        {"role": "user", "content": "Thanks"},
    ]


async def test_api_sent_text_is_in_text_agent_history_only(async_session) -> None:
    org, agent, _number = await _seed(async_session)
    base = datetime.now(timezone.utc) - timedelta(minutes=10)

    def row(org_id, frm, to, body, secs, agent_id=None):
        return Sms(
            organization_id=org_id,
            agent_id=agent_id,
            from_e164=frm,
            to_e164=to,
            direction="outbound" if frm == ORG_NUMBER else "inbound",
            status="sent",
            body=body,
            requested_at=base + timedelta(seconds=secs),
        )

    other_org = uuid.uuid4()
    async_session.add_all(
        [
            row(org, ORG_NUMBER, PERSON, "api out", 1),
            row(org, PERSON, ORG_NUMBER, "api in", 2),
            row(org, ORG_NUMBER, "+33600000000", "other caller", 3),
            row(org, "+14155550999", PERSON, "other org number", 4),
            row(other_org, ORG_NUMBER, PERSON, "other org", 5),
        ]
    )
    await async_session.commit()
    result = await _ingest(async_session, "now?", "SM-API1")
    sms = await async_session.get(Sms, result.sms_id)

    history = await text_agent.thread_history_for_reply(async_session, sms, agent)
    assert [i.text for i in history] == ["api out", "api in", "now?"]

    voice = await threads.thread_items(async_session, org, agent.id, PERSON)
    assert [i.text for i in voice if i.kind.startswith("text")] == ["now?"]
