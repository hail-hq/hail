"""Tests for the call-lifecycle webhook fan-out wired into ``agent.py``.

Pure mapping tests plus behavioral tests reusing the real ``async_session``
DB harness (see ``voicebot/tests/conftest.py`` /
``hailhq.core.testing.fixtures``) already exercised by ``test_agent.py``'s
``mark_call_answered`` / ``on_call_end`` tests.
"""

from __future__ import annotations

from uuid import UUID

from hailhq.core.models import Call, PhoneNumber, WebhookDelivery, WebhookSubscription
from hailhq.voicebot.agent import (
    _STATUS_TO_CALL_EVENT,
    mark_call_answered,
    on_call_end,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


def test_status_to_call_event_covers_reachable_terminals() -> None:
    assert _STATUS_TO_CALL_EVENT["in_progress"] == "call.answered"
    assert _STATUS_TO_CALL_EVENT["completed"] == "call.completed"
    assert _STATUS_TO_CALL_EVENT["failed"] == "call.failed"
    assert _STATUS_TO_CALL_EVENT["busy"] == "call.busy"
    assert _STATUS_TO_CALL_EVENT["no_answer"] == "call.no_answer"


def test_status_to_call_event_excludes_unreachable() -> None:
    # ringing/canceled have no data source and must not be emittable.
    assert "ringing" not in _STATUS_TO_CALL_EVENT
    assert "canceled" not in _STATUS_TO_CALL_EVENT


async def _make_call_row(session: AsyncSession, org_id: UUID) -> UUID:
    """Insert a phone_number + queued call against ``org_id``.

    Mirrors ``test_agent.py``'s ``_make_call_row`` but takes an explicit
    ``org_id`` so each test can seed a matching ``WebhookSubscription``.
    """
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155551234",
        country_code="US",
        number_type="local",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    call = Call(
        organization_id=org_id,
        from_number_id=pn.id,
        from_e164=pn.e164,
        to_e164="+14155559999",
        voice_config={"stt": "deepgram", "tts": "cartesia"},
        status="dialing",
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call.id


async def _seed_subscription(
    session: AsyncSession, org_id: UUID, event_types: list[str]
) -> None:
    session.add(
        WebhookSubscription(
            organization_id=org_id,
            target_url="https://example.com/firehose",
            secret_encrypted="hash",
            event_types=event_types,
        )
    )
    await session.commit()


async def test_mark_call_answered_emits_call_answered_delivery(
    async_session: AsyncSession,
) -> None:
    """A real dialing -> in_progress transition fans out one call.answered row."""
    org_id = UUID("aaaaaaaa-1111-1111-1111-111111111111")
    await _seed_subscription(async_session, org_id, ["call.answered"])
    call_id = await _make_call_row(async_session, org_id)

    transitioned = await mark_call_answered(call_id)
    assert transitioned is True

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "call.answered"
    assert rows[0].event_id == call_id


async def test_mark_call_answered_noop_emits_no_delivery(
    async_session: AsyncSession,
) -> None:
    """A second (already-answered) signal must not fan out a duplicate event."""
    org_id = UUID("aaaaaaaa-2222-2222-2222-222222222222")
    await _seed_subscription(async_session, org_id, ["call.answered"])
    call_id = await _make_call_row(async_session, org_id)

    assert await mark_call_answered(call_id) is True
    assert await mark_call_answered(call_id) is False

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1  # only the first (real) transition fanned out


async def test_on_call_end_emits_call_completed_delivery(
    async_session: AsyncSession,
) -> None:
    """A real terminal transition fans out exactly one call.completed row."""
    org_id = UUID("aaaaaaaa-3333-3333-3333-333333333333")
    await _seed_subscription(async_session, org_id, ["call.completed"])
    call_id = await _make_call_row(async_session, org_id)

    await on_call_end(call_id, room_name=f"hail-{call_id}")

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1
    assert rows[0].event_type == "call.completed"
    assert rows[0].event_id == call_id


async def test_on_call_end_noop_on_already_terminal_emits_no_delivery(
    async_session: AsyncSession,
) -> None:
    """A second on_call_end against an already-terminal row fans out nothing."""
    org_id = UUID("aaaaaaaa-4444-4444-4444-444444444444")
    await _seed_subscription(async_session, org_id, ["call.completed"])
    call_id = await _make_call_row(async_session, org_id)

    await on_call_end(call_id, room_name=f"hail-{call_id}")
    await on_call_end(call_id, room_name=f"hail-{call_id}")  # already terminal

    rows = (await async_session.execute(select(WebhookDelivery))).scalars().all()
    assert len(rows) == 1  # only the first (real) transition fanned out
