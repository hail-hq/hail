"""Text agent: queueing on ingest, claiming, history, and the loop breaker."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core import text_agent
from hailhq.core.models import Agent, PhoneNumber, Sms
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
            requested_at=base + timedelta(seconds=22),
        )
    )
    await async_session.commit()
    result = await _ingest(async_session, "Tuesday then?", "SM3")

    claimed = await text_agent.claim_pending_reply(async_session)
    assert claimed is not None
    assert claimed.sms.id == result.sms_id
    assert claimed.agent.id == agent.id

    history = await text_agent.thread_messages(async_session, claimed.sms)
    assert len(history) == text_agent.THREAD_LIMIT
    assert history[-1].body == "Tuesday then?"
    assert history[-2].body == "Sure."
    assert next(h.body for h in history) == "msg 4"  # oldest kept, in order

    messages = text_agent.build_chat_messages(agent, history)
    assert messages[0]["role"] == "system"
    assert "Book appointments." in messages[0]["content"]
    assert text_agent.TEXT_PREAMBLE in messages[0]["content"]
    assert messages[-1] == {"role": "user", "content": "Tuesday then?"}
    assert messages[-2] == {"role": "assistant", "content": "Sure."}

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
