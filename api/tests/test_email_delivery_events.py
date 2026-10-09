"""apply_delivery_event: dedup, guarded status transitions, fanout."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from hailhq.core.email_delivery_events import apply_delivery_event
from hailhq.core.models import (
    Email,
    EmailEvent,
    EmailForwardTarget,
    OrganizationMember,
    User,
)
from hailhq.core.providers.email.inbound.ses_delivery import DeliveryEvent
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

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


async def _mk_forward(session: AsyncSession, *, to: str) -> Email:
    """An outbound row that ingest-driven forwarding wrote (metadata.forwarded_from)."""
    email = await _mk_email(session, status="delivered")
    email.to_addresses = [to]
    email.metadata_ = {"forwarded_from": str(uuid4()), "forward_headers": {}}
    await session.commit()
    await session.refresh(email)
    return email


async def _add_owner(session: AsyncSession, org_id: UUID, email: str, *, verified=True):
    uid = uuid4()
    session.add(
        User(
            id=uid,
            name="Owner",
            email=email,
            email_verified=verified,
            created_at=T0,
        )
    )
    session.add(
        OrganizationMember(
            id=uuid4(), user_id=uid, organization_id=org_id, role="owner", created_at=T0
        )
    )
    await session.commit()


async def test_complaint_on_forward_stops_target_and_notifies_owner(async_session):
    email = await _mk_forward(async_session, to="Victim@Example.com")
    org_id = email.organization_id
    async_session.add(
        EmailForwardTarget(
            organization_id=org_id, address="victim@example.com", status="verified"
        )
    )
    await async_session.commit()
    await _add_owner(async_session, org_id, "owner@acme.com")
    await _add_owner(async_session, org_id, "unverified@acme.com", verified=False)

    fanout = AsyncMock(return_value=0)
    res = await apply_delivery_event(
        async_session,
        _ev(
            email.provider_message_id,
            "complained",
            detail={
                "complaint_feedback_type": "abuse",
                "recipients": ["victim@example.com"],
            },
        ),
        fanout=fanout,
    )
    await async_session.commit()
    assert res.inserted

    target = (
        await async_session.execute(
            select(EmailForwardTarget).where(
                EmailForwardTarget.organization_id == org_id
            )
        )
    ).scalar_one()
    assert target.status == "stopped"
    assert target.stopped_reason == "complaint"
    assert target.stopped_email_id == email.id

    notices = (
        (
            await async_session.execute(
                select(Email).where(
                    Email.organization_id == org_id,
                    Email.metadata_["system_kind"].astext == "forward_stopped",
                )
            )
        )
        .scalars()
        .all()
    )
    assert [n.to_addresses for n in notices] == [["owner@acme.com"]]
    assert notices[0].status == "queued"
    assert notices[0].email_domain_id == email.email_domain_id
    assert "victim@example.com" in notices[0].body_text


async def test_complaint_redelivery_does_not_notify_twice(async_session):
    email = await _mk_forward(async_session, to="victim@example.com")
    org_id = email.organization_id
    await _add_owner(async_session, org_id, "owner@acme.com")
    ev = _ev(email.provider_message_id, "complained", detail={"recipients": []})
    fanout = AsyncMock(return_value=0)
    await apply_delivery_event(async_session, ev, fanout=fanout)
    await async_session.commit()
    await apply_delivery_event(async_session, ev, fanout=fanout)
    await async_session.commit()

    notices = (
        (
            await async_session.execute(
                select(Email).where(Email.metadata_["system_kind"].astext.isnot(None))
            )
        )
        .scalars()
        .all()
    )
    assert len(notices) == 1
    # No prior row for the address: one is created stopped so the console can show why.
    target = (
        await async_session.execute(
            select(EmailForwardTarget).where(
                EmailForwardTarget.organization_id == org_id
            )
        )
    ).scalar_one()
    assert (target.address, target.status) == ("victim@example.com", "stopped")


async def test_complaint_on_direct_send_leaves_forward_targets_alone(async_session):
    email = await _mk_email(async_session, status="delivered")
    async_session.add(
        EmailForwardTarget(
            organization_id=email.organization_id,
            address="bob@example.com",
            status="verified",
        )
    )
    await async_session.commit()
    fanout = AsyncMock(return_value=0)
    await apply_delivery_event(
        async_session, _ev(email.provider_message_id, "complained"), fanout=fanout
    )
    await async_session.commit()
    target = (
        await async_session.execute(
            select(EmailForwardTarget).where(
                EmailForwardTarget.organization_id == email.organization_id
            )
        )
    ).scalar_one()
    assert target.status == "verified"
