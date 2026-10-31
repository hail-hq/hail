"""Forward-target state machine: sync, confirm, resend cooldown, stop."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hailhq.core.forward_targets import (
    MAX_CONFIRM_MAILS_PER_ADDRESS_PER_DAY,
    MAX_CONFIRM_MAILS_PER_DAY,
    MAX_PENDING_TARGETS,
    RESEND_COOLDOWN,
    TOKEN_TTL,
    AlreadyVerified,
    ConfirmBudgetExceeded,
    PendingLimitExceeded,
    ResendTooSoon,
    confirm_target,
    find_by_token,
    list_targets,
    promote_verified_members,
    reissue_token,
    statuses_for,
    stop_targets,
    sync_targets,
)
from hailhq.core.models import (
    Email,
    EmailDomain,
    EmailForwardTarget,
    OrganizationMember,
    User,
)
from sqlalchemy import select

NOW = datetime.now(UTC)


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

    # The unverified member gets a pending row but NO confirm mail: the
    # account verify click promotes it instead.
    assert [i.target.address for i in issued] == ["stranger@example.com"]
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
    # reissue_token re-reads the row under the org lock: persist the edit.
    await async_session.flush()
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


@pytest.mark.asyncio
async def test_pending_cap_blocks_a_second_list_of_strangers(async_session):
    """Saving a fresh list of strangers on every PATCH must not turn Hail into
    a confirm-mail cannon: unconfirmed rows count across saves."""
    org_id = uuid.uuid4()
    first = [f"s{i}@example.com" for i in range(MAX_PENDING_TARGETS)]
    issued = await sync_targets(async_session, org_id, first)
    await async_session.commit()
    assert len(issued) == MAX_PENDING_TARGETS

    with pytest.raises(PendingLimitExceeded):
        await sync_targets(async_session, org_id, ["one-more@example.com"])
    # Nothing was written by the rejected save.
    assert len(await list_targets(async_session, org_id)) == MAX_PENDING_TARGETS
    # A verified member is not a stranger and still goes through.
    await _member(async_session, org_id, "me@acme.com")
    assert await sync_targets(async_session, org_id, ["me@acme.com"]) == []


async def _queued_confirm_mails(session, org_id, n, *, to=None):
    dom = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain=f"a{org_id.hex[:8]}+{org_id.hex[:8]}@mail.hail.so",
        local_prefix_user=f"a{org_id.hex[:8]}",
        local_prefix_org=org_id.hex[:8],
        verification_status="verified",
        provider="ses",
    )
    session.add(dom)
    await session.flush()
    for i in range(n):
        session.add(
            Email(
                organization_id=org_id,
                email_domain_id=dom.id,
                direction="outbound",
                from_address="noreply+b@mail.hail.so",
                to_addresses=[to or f"x{i}@example.com"],
                subject="Confirm email forwarding from Hail",
                body_text="confirm",
                status="sent",
                provider="ses",
                metadata_={"system_kind": "forward_confirm"},
            )
        )
    await session.flush()


@pytest.mark.asyncio
async def test_daily_confirm_budget_blocks_new_targets_and_resends(async_session):
    org_id = uuid.uuid4()
    (issued,) = await sync_targets(async_session, org_id, ["a@example.com"])
    await _queued_confirm_mails(async_session, org_id, MAX_CONFIRM_MAILS_PER_DAY)
    await async_session.commit()

    with pytest.raises(ConfirmBudgetExceeded) as exc:
        await sync_targets(async_session, org_id, ["b@example.com"])
    assert exc.value.scope == "org"
    issued.target.token_sent_at = NOW - RESEND_COOLDOWN - timedelta(seconds=1)
    await async_session.flush()
    with pytest.raises(ConfirmBudgetExceeded):
        await reissue_token(async_session, issued.target)


@pytest.mark.asyncio
async def test_per_address_daily_cap_counts_every_org(async_session):
    """One account can own several orgs; the per-org caps must not add up
    against one recipient. But an org's first mail to an address always goes
    through, so no org can lock an address for everyone else."""
    victim = "victim@example.com"
    other_org = uuid.uuid4()
    await _queued_confirm_mails(
        async_session, other_org, MAX_CONFIRM_MAILS_PER_ADDRESS_PER_DAY, to=victim
    )
    await async_session.commit()

    # First mail from a fresh org: allowed even though the address is "full".
    org_id = uuid.uuid4()
    (issued,) = await sync_targets(async_session, org_id, [victim])
    assert issued.target.address == victim
    await _queued_confirm_mails(async_session, org_id, 1, to=victim)
    await async_session.commit()

    # A repeat (resend) from that org is refused; the text names no counts.
    issued.target.token_sent_at = NOW - RESEND_COOLDOWN - timedelta(seconds=1)
    await async_session.flush()
    with pytest.raises(ConfirmBudgetExceeded) as exc:
        await reissue_token(async_session, issued.target)
    assert (exc.value.scope, exc.value.address) == ("address", victim)
    assert "received" not in str(exc.value) and "3" not in str(exc.value)

    # Another address in the same org is unaffected.
    (fine,) = await sync_targets(async_session, org_id, ["fine@example.com"])
    assert fine.target.address == "fine@example.com"


@pytest.mark.asyncio
async def test_unverified_member_row_goes_live_on_account_verification(async_session):
    org_id = uuid.uuid4()
    await _member(async_session, org_id, "me@acme.com", verified=False)
    issued = await sync_targets(async_session, org_id, ["me@acme.com"])
    await async_session.commit()
    assert issued == []
    (row,) = await list_targets(async_session, org_id)
    assert (row.status, row.token_sent_at, row.token_hash) == ("pending", None, None)
    assert await statuses_for(async_session, org_id, ["me@acme.com"]) == {
        "me@acme.com": "pending"
    }

    user = (
        await async_session.execute(select(User).where(User.email == "me@acme.com"))
    ).scalar_one()
    user.email_verified = True
    await async_session.commit()

    # Both read paths promote: ingest's status lookup and the console list.
    assert await statuses_for(async_session, org_id, ["me@acme.com"]) == {
        "me@acme.com": "verified"
    }
    await async_session.commit()
    (row,) = await list_targets(async_session, org_id)
    assert row.status == "verified" and row.verified_at is not None
    assert await promote_verified_members(async_session, org_id) == 0


@pytest.mark.asyncio
async def test_stopped_member_row_is_not_promoted(async_session):
    org_id = uuid.uuid4()
    await _member(async_session, org_id, "me@acme.com")
    await sync_targets(async_session, org_id, ["me@acme.com"])
    await stop_targets(
        async_session, org_id, ["me@acme.com"], reason="complaint", email_id=None
    )
    await async_session.commit()
    assert await promote_verified_members(async_session, org_id) == 0
    assert await statuses_for(async_session, org_id, ["me@acme.com"]) == {
        "me@acme.com": "stopped"
    }
