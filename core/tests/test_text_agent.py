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
    org, agent, number = await _seed(async_session)
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
