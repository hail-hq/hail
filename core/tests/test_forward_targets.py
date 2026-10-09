"""Forward-target state machine: sync, confirm, resend cooldown, stop."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core.forward_targets import (
    RESEND_COOLDOWN,
    TOKEN_TTL,
    AlreadyVerified,
    ResendTooSoon,
    confirm_target,
    find_by_token,
    list_targets,
    reissue_token,
    statuses_for,
    stop_targets,
    sync_targets,
)
from hailhq.core.models import EmailForwardTarget, OrganizationMember, User

NOW = datetime.now(timezone.utc)


async def _member(session, org_id, email, *, verified=True):
    uid = uuid.uuid4()
    session.add(
        User(id=uid, name="M", email=email, email_verified=verified, created_at=NOW)
    )
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=uid,
            organization_id=org_id,
            role="owner",
            created_at=NOW,
        )
    )
    await session.flush()


@pytest.mark.asyncio
async def test_sync_auto_verifies_verified_member_and_issues_token_for_stranger(
    async_session,
):
    org_id = uuid.uuid4()
    await _member(async_session, org_id, "Me@Acme.com")
    await _member(async_session, org_id, "unverified@acme.com", verified=False)

    issued = await sync_targets(
        async_session,
        org_id,
        [" ME@acme.com ", "stranger@example.com", "unverified@acme.com"],
    )
    await async_session.commit()

    assert sorted(i.target.address for i in issued) == [
        "stranger@example.com",
        "unverified@acme.com",
    ]
    assert all(len(i.raw_token) > 20 for i in issued)
    statuses = await statuses_for(
        async_session,
        org_id,
        ["me@acme.com", "stranger@example.com", "unverified@acme.com"],
    )
    assert statuses == {
        "me@acme.com": "verified",
        "stranger@example.com": "pending",
        "unverified@acme.com": "pending",
    }


@pytest.mark.asyncio
async def test_sync_is_idempotent_and_never_reissues(async_session):
    org_id = uuid.uuid4()
    first = await sync_targets(async_session, org_id, ["a@example.com"])
    await async_session.commit()
    again = await sync_targets(
        async_session, org_id, ["A@example.com", "a@example.com"]
    )
    await async_session.commit()
    assert len(first) == 1 and again == []
    rows = await list_targets(async_session, org_id)
    assert [r.address for r in rows] == ["a@example.com"]
    # The original token still confirms.
    assert (await find_by_token(async_session, first[0].raw_token)) is not None


@pytest.mark.asyncio
async def test_confirm_marks_verified_and_consumes_token(async_session):
    org_id = uuid.uuid4()
    (issued,) = await sync_targets(async_session, org_id, ["a@example.com"])
    await async_session.commit()

    assert await find_by_token(async_session, "nope") is None
    target = await find_by_token(async_session, issued.raw_token)
    assert target is not None
    await confirm_target(async_session, target)
    await async_session.commit()

    assert target.status == "verified" and target.verified_at is not None
    assert target.token_hash is None
    assert await find_by_token(async_session, issued.raw_token) is None


@pytest.mark.asyncio
async def test_expired_token_does_not_confirm(async_session):
    org_id = uuid.uuid4()
    (issued,) = await sync_targets(async_session, org_id, ["a@example.com"])
    issued.target.token_expires_at = NOW - timedelta(seconds=1)
    await async_session.commit()
    assert await find_by_token(async_session, issued.raw_token) is None
    assert TOKEN_TTL == timedelta(days=7)


@pytest.mark.asyncio
async def test_reissue_respects_cooldown_and_verified(async_session):
    org_id = uuid.uuid4()
    (issued,) = await sync_targets(async_session, org_id, ["a@example.com"])
    await async_session.commit()
    target = issued.target

    with pytest.raises(ResendTooSoon) as exc:
        await reissue_token(async_session, target)
    assert timedelta(0) < exc.value.retry_after <= RESEND_COOLDOWN

    target.token_sent_at = NOW - RESEND_COOLDOWN - timedelta(seconds=1)
    raw = await reissue_token(async_session, target)
    await async_session.commit()
    assert raw != issued.raw_token
    assert await find_by_token(async_session, issued.raw_token) is None
    assert await find_by_token(async_session, raw) is target

    await confirm_target(async_session, target)
    with pytest.raises(AlreadyVerified):
        await reissue_token(async_session, target)


@pytest.mark.asyncio
async def test_stop_then_reconfirm(async_session):
    org_id = uuid.uuid4()
    await _member(async_session, org_id, "me@acme.com")
    await sync_targets(async_session, org_id, ["me@acme.com"])
    await async_session.commit()

    stopped = await stop_targets(
        async_session,
        org_id,
        ["ME@acme.com", "never-seen@example.com"],
        reason="complaint",
        email_id=None,
    )
    await async_session.commit()
    assert sorted((t.address, t.status) for t in stopped) == [
        ("me@acme.com", "stopped"),
        ("never-seen@example.com", "stopped"),
    ]
    me = next(t for t in stopped if t.address == "me@acme.com")
    assert me.stopped_reason == "complaint" and me.stopped_at is not None

    # A stopped address is restarted only by a fresh link: sync leaves it alone.
    assert await sync_targets(async_session, org_id, ["me@acme.com"]) == []
    raw = await reissue_token(async_session, me)
    target = await find_by_token(async_session, raw)
    assert target is me
    await confirm_target(async_session, me)
    await async_session.commit()
    assert me.status == "verified" and me.stopped_at is None
    assert isinstance(me, EmailForwardTarget)
