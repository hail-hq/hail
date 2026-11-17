"""Call transcript and the call.analyzed webhook: picking, sending once."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core.call_analysis import (
    ANALYZED_KIND,
    call_transcript,
    next_call_to_analyze,
    send_call_analyzed,
)
from hailhq.core.models import (
    Agent,
    Call,
    CallEvent,
    PhoneNumber,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.threads import TEXT_MARKER
from hailhq.core.webhook_events import WebhookEventType
from sqlalchemy import func, select

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


async def _seed_call(
    session,
    *,
    org: uuid.UUID | None = None,
    ended_ago: timedelta = timedelta(minutes=1),
    turns: list[tuple[str, str]] | None = None,
    subscribe: bool = True,
    answered: bool = True,
) -> Call:
    org = org or uuid.uuid4()
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Help.")
    session.add(agent)
    await session.flush()
    number = PhoneNumber(
        organization_id=org,
        e164=f"+1415555{uuid.uuid4().int % 10_000:04d}",
        country_code="US",
        number_type="local",
        provisioning_state="active",
        provider_resource_id=f"PN{uuid.uuid4().hex}",
    )
    session.add(number)
    await session.flush()
    ended_at = datetime.now(timezone.utc) - ended_ago
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        from_e164=PERSON,
        to_e164=number.e164,
        direction="inbound",
        status="completed",
        end_reason="normal_hangup",
        provider="twilio",
        voice_config={},
        answered_at=ended_at - timedelta(seconds=74) if answered else None,
        ended_at=ended_at,
    )
    session.add(call)
    await session.flush()
    start = ended_at - timedelta(seconds=60)
    for i, (kind, text) in enumerate(
        turns
        if turns is not None
        else [
            ("agent_turn", "Hi, this is Acme."),
            ("user_turn", "I'd like to move my appointment."),
        ]
    ):
        session.add(
            CallEvent(
                call_id=call.id,
                kind=kind,
                payload={
                    "role": "user" if kind == "user_turn" else "assistant",
                    "text": text,
                },
                occurred_at=start + timedelta(seconds=i),
            )
        )
    if subscribe:
        session.add(
            WebhookSubscription(
                organization_id=org,
                target_url="https://example.com/hook",
                event_types=[WebhookEventType.CALL_ANALYZED.value],
                secret_encrypted="x",
                created_at=ended_at - timedelta(hours=1),
                updated_at=ended_at - timedelta(hours=1),
            )
        )
    await session.commit()
    return call


async def _deliveries(session, call_id) -> list[WebhookDelivery]:
    return list(
        (
            await session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "call.analyzed",
                    WebhookDelivery.event_id == call_id,
                )
            )
        ).scalars()
    )


async def test_transcript_is_ordered_and_skips_blank_and_texts(async_session) -> None:
    call = await _seed_call(
        async_session,
        turns=[
            ("agent_turn", "Hi, this is Acme."),
            ("user_turn", "   "),
            ("user_turn", TEXT_MARKER + "running late"),
            ("voice_metrics", "not a turn"),
            ("user_turn", "Can I come at ten?"),
        ],
    )
    turns = await call_transcript(async_session, call.id)
    assert [(t["role"], t["text"]) for t in turns] == [
        ("assistant", "Hi, this is Acme."),
        ("user", "Can I come at ten?"),
    ]
    assert all(datetime.fromisoformat(t["at"]) for t in turns)


async def test_event_type_is_registered() -> None:
    assert WebhookEventType("call.analyzed").channel == "Calls"


async def test_picks_an_ended_subscribed_call(async_session) -> None:
    call = await _seed_call(async_session)
    picked = await next_call_to_analyze(async_session)
    assert picked is not None and picked.id == call.id
    assert await next_call_to_analyze(async_session, exclude={call.id}) is None


async def test_skips_calls_it_must_not_analyze(async_session) -> None:
    await _seed_call(async_session, subscribe=False)
    await _seed_call(async_session, ended_ago=timedelta(hours=25))
    await _seed_call(async_session, ended_ago=timedelta(seconds=1))
    await _seed_call(async_session, turns=[])
    assert await next_call_to_analyze(async_session) is None


async def test_subscription_created_after_the_call_is_not_enough(
    async_session,
) -> None:
    call = await _seed_call(async_session, subscribe=False)
    async_session.add(
        WebhookSubscription(
            organization_id=call.organization_id,
            target_url="https://example.com/late",
            event_types=["call.analyzed"],
            secret_encrypted="x",
        )
    )
    await async_session.commit()
    assert await next_call_to_analyze(async_session) is None


async def test_adding_the_event_to_an_old_subscription_does_not_replay(
    async_session,
) -> None:
    call = await _seed_call(async_session, subscribe=False)
    sub = WebhookSubscription(
        organization_id=call.organization_id,
        target_url="https://example.com/old",
        event_types=["call.completed"],
        secret_encrypted="x",
        created_at=call.ended_at - timedelta(days=3),
        updated_at=call.ended_at - timedelta(days=3),
    )
    async_session.add(sub)
    await async_session.commit()
    # What PATCH /v1/webhooks/{id} writes when the event is added.
    sub.event_types = ["call.completed", "call.analyzed"]
    sub.updated_at = datetime.now(timezone.utc)
    await async_session.commit()
    assert await next_call_to_analyze(async_session) is None


async def test_disabled_or_other_event_subscription_is_not_enough(
    async_session,
) -> None:
    call = await _seed_call(async_session, subscribe=False)
    async_session.add_all(
        [
            WebhookSubscription(
                organization_id=call.organization_id,
                target_url="https://example.com/a",
                event_types=["call.completed"],
                secret_encrypted="x",
                created_at=call.ended_at - timedelta(hours=1),
                updated_at=call.ended_at - timedelta(hours=1),
            ),
            WebhookSubscription(
                organization_id=call.organization_id,
                target_url="https://example.com/b",
                event_types=["call.analyzed"],
                secret_encrypted="x",
                status="disabled",
                created_at=call.ended_at - timedelta(hours=1),
                updated_at=call.ended_at - timedelta(hours=1),
            ),
        ]
    )
    await async_session.commit()
    assert await next_call_to_analyze(async_session) is None


async def test_send_enqueues_one_delivery_with_the_payload(async_session) -> None:
    call = await _seed_call(async_session)
    turns = await call_transcript(async_session, call.id)
    assert await send_call_analyzed(async_session, call.id, turns, "Moved to Thursday.")
    [delivery] = await _deliveries(async_session, call.id)
    data = delivery.payload["data"]
    assert data["id"] == str(call.id)
    assert data["direction"] == "inbound"
    assert data["from"] == PERSON
    assert data["to"] == call.to_e164
    assert data["agent_id"] == str(call.agent_id)
    assert data["end_reason"] == "normal_hangup"
    assert data["duration_seconds"] == 74
    assert data["summary"] == "Moved to Thursday."
    assert [t["text"] for t in data["transcript"]] == [
        "Hi, this is Acme.",
        "I'd like to move my appointment.",
    ]
    marker = (
        await async_session.execute(
            select(CallEvent).where(
                CallEvent.call_id == call.id, CallEvent.kind == ANALYZED_KIND
            )
        )
    ).scalar_one()
    # The event stream gets counts, never the text.
    assert marker.payload == {"turns": 2, "summary": True}
    # Sent once: no second delivery, and the call is not picked again.
    call_id = call.id  # the refused send rolls back, expiring ``call``
    assert not await send_call_analyzed(async_session, call_id, turns, "again")
    assert len(await _deliveries(async_session, call_id)) == 1
    assert await next_call_to_analyze(async_session) is None


async def test_summary_null_and_unanswered_duration(async_session) -> None:
    call = await _seed_call(async_session, answered=False)
    turns = await call_transcript(async_session, call.id)
    assert await send_call_analyzed(async_session, call.id, turns, None)
    [delivery] = await _deliveries(async_session, call.id)
    assert delivery.payload["data"]["summary"] is None
    assert delivery.payload["data"]["duration_seconds"] is None


async def test_two_senders_race_one_delivery(session_factory) -> None:
    async with session_factory() as s:
        call = await _seed_call(s)

    async def send() -> bool:
        async with session_factory() as s:
            turns = await call_transcript(s, call.id)
            return await send_call_analyzed(s, call.id, turns, "Summary.")

    results = await asyncio.gather(send(), send())
    assert sorted(results) == [False, True]
    async with session_factory() as s:
        assert len(await _deliveries(s, call.id)) == 1
        count = await s.scalar(
            select(func.count())
            .select_from(CallEvent)
            .where(CallEvent.call_id == call.id, CallEvent.kind == ANALYZED_KIND)
        )
        assert count == 1


async def test_empty_after_filtering_is_marked_not_sent(async_session) -> None:
    call = await _seed_call(async_session, turns=[("user_turn", "  ")])
    call_id = call.id
    assert (await next_call_to_analyze(async_session)).id == call_id
    turns = await call_transcript(async_session, call_id)
    assert turns == []
    assert not await send_call_analyzed(async_session, call_id, turns, None)
    assert await _deliveries(async_session, call_id) == []
    assert await next_call_to_analyze(async_session) is None
