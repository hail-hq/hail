"""apply_delivery_event: dedup, guarded status transitions, fanout."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hailhq.core.email_delivery_events import apply_delivery_event
from hailhq.core.models import Email, EmailEvent
from hailhq.core.providers.email.inbound.ses_delivery import DeliveryEvent

T0 = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


async def _mk_email(session: AsyncSession, *, status="sent", pmid=None) -> Email:
    email = Email(
        organization_id=uuid4(),
        email_domain_id=None,
        direction="outbound",
        from_address="noreply@acme.com",
        to_addresses=["bob@example.com"],
        subject="hi",
        body_text="hello",
        status=status,
        provider="ses",
        provider_message_id=pmid or f"pmid-{uuid4()}",
    )
    # Outbound rows require email_domain_id (emails_outbound_has_domain
    # CHECK) — create a minimal verified EmailDomain to satisfy it.
    from hailhq.core.models import EmailDomain

    dom = EmailDomain(
        organization_id=email.organization_id,
        kind="custom",
        domain=f"acme-{uuid4().hex[:8]}.com",
        verification_status="verified",
        dns_records=[],
        provider="ses",
    )
    session.add(dom)
    await session.flush()
    email.email_domain_id = dom.id
    session.add(email)
    await session.commit()
    await session.refresh(email)
    return email


def _ev(pmid: str, kind: str, ts=T0, detail=None) -> DeliveryEvent:
    return DeliveryEvent(
        kind=kind,
        provider_message_id=pmid,
        occurred_at=ts,
        detail=detail if detail is not None else {},
    )


async def test_delivery_inserts_event_and_advances_status(async_session):
    email = await _mk_email(async_session, status="sent")
    fanout = AsyncMock(return_value=1)
    res = await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "delivered", detail={"smtp_response": "250"}),
        fanout=fanout,
    )
    await async_session.commit()
    assert res.inserted and res.status_changed
    await async_session.refresh(email)
    assert email.status == "delivered"
    fanout.assert_awaited_once()
    assert fanout.await_args.kwargs["event_type"] == "email.delivered"


async def test_duplicate_event_skips_fanout(async_session):
    email = await _mk_email(async_session, status="sent")
    fanout = AsyncMock(return_value=1)
    ev = _ev(email.provider_message_id, "delivered")
    await apply_delivery_event(async_session, ev, fanout=fanout)
    await async_session.commit()
    res2 = await apply_delivery_event(async_session, ev, fanout=fanout)
    await async_session.commit()
    assert not res2.inserted
    assert fanout.await_count == 1
    rows = (await async_session.execute(select(EmailEvent))).scalars().all()
    assert len(rows) == 1


async def test_soft_bounce_records_event_without_status_change(async_session):
    email = await _mk_email(async_session, status="delivered")
    fanout = AsyncMock(return_value=0)
    res = await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "bounced", detail={"hard": False}),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert res.inserted and not res.status_changed
    assert email.status == "delivered"


async def test_hard_bounce_overrides_delivered_but_not_complained(async_session):
    email = await _mk_email(async_session, status="delivered")
    fanout = AsyncMock(return_value=0)
    await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "bounced", detail={"hard": True}),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "bounced"

    # complaint still wins over bounced
    await apply_delivery_event(
        async_session, _ev(email.provider_message_id, "complained"), fanout=fanout
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "complained"

    # late delivered never regresses a terminal state
    await apply_delivery_event(
        async_session,
        _ev(
            email.provider_message_id,
            "delivered",
            ts=datetime(2026, 7, 1, 13, 0, tzinfo=timezone.utc),
        ),
        fanout=fanout,
    )
    await async_session.commit()
    await async_session.refresh(email)
    assert email.status == "complained"


async def test_rejected_sets_failed_with_end_reason_and_no_fanout(async_session):
    email = await _mk_email(async_session, status="sent")
    fanout = AsyncMock(return_value=0)
    res = await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "rejected", detail={"reason": "Bad content"}),
        fanout=fanout,
    )
    await async_session.commit()
    assert res.email_id == email.id
    assert res.inserted
    assert res.status_changed
    await async_session.refresh(email)
    assert email.status == "failed"
    assert email.end_reason == "Bad content"
    assert email.failed_at is not None
    fanout.assert_not_awaited()


async def test_delay_and_engagement_kinds_fan_out_without_status_change(
    async_session,
):
    email = await _mk_email(async_session, status="delivered")
    fanout = AsyncMock(return_value=1)
    for kind in ("delivery_delayed", "opened", "clicked"):
        res = await apply_delivery_event(
            async_session,
            _ev(email.provider_message_id, kind),
            fanout=fanout,
        )
        await async_session.commit()
        assert res.inserted
        assert not res.status_changed
    assert [c.kwargs["event_type"] for c in fanout.await_args_list] == [
        "email.delivery_delayed",
        "email.opened",
        "email.clicked",
    ]
    await async_session.refresh(email)
    assert email.status == "delivered"


async def test_concurrent_status_change_yields_status_changed_false(
    async_session, session_factory: async_sessionmaker[AsyncSession]
):
    """Guarded UPDATE's ``WHERE status IN (...)`` must catch a concurrent
    status change that the current session can't see in memory.

    Mechanism exercised: ``async_session`` is built with
    ``expire_on_commit=False`` (see hailhq.core.testing.fixtures.db), so
    after ``_mk_email``'s commit+refresh, the ``email`` instance sits in
    ``async_session``'s identity map fully loaded and unexpired
    (status="sent"). A second, independent session then updates the same
    row to "complained" in the database and commits. When
    ``apply_delivery_event`` issues its own ``select(Email)`` on
    ``async_session``, SQLAlchemy's identity map returns the *same* Python
    object without re-populating its attributes from the new row — so the
    Python-level pre-check in ``_new_status_for`` still sees "sent" and
    decides a transition to "delivered" is allowed. Only the SQL-level
    guard (``WHERE status IN ('sent')``) catches the mismatch, matching 0
    rows because the real DB status is now "complained". This is exactly
    the 0-rows-matched branch (``status_changed = result.rowcount == 1``)
    the guard exists for.
    """
    email = await _mk_email(async_session, status="sent")

    async with session_factory() as other:
        await other.execute(
            update(Email).where(Email.id == email.id).values(status="complained")
        )
        await other.commit()

    fanout = AsyncMock(return_value=0)
    res = await apply_delivery_event(
        async_session,
        _ev(email.provider_message_id, "delivered"),
        fanout=fanout,
    )
    await async_session.commit()

    assert res.inserted is True
    assert res.status_changed is False

    async with session_factory() as check:
        refreshed = (
            await check.execute(select(Email).where(Email.id == email.id))
        ).scalar_one()
        assert refreshed.status == "complained"  # never regressed to "delivered"

    rows = (
        (
            await async_session.execute(
                select(EmailEvent).where(EmailEvent.email_id == email.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].kind == "delivered"


async def test_unmatched_provider_message_id_is_noop(async_session):
    fanout = AsyncMock()
    res = await apply_delivery_event(
        async_session, _ev("pmid-does-not-exist", "delivered"), fanout=fanout
    )
    assert res.email_id is None and not res.inserted
    fanout.assert_not_awaited()
