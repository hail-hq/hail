"""Pool helpers — claim atomicity + release idempotency + sweeper backstop."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import update

from hailhq.core.models import Call, PhoneNumber
from hailhq.core.pool import (
    claim_pool_number,
    release_pool_reservation,
    sweep_pool_reservations,
)


async def _make_number(
    session,
    *,
    e164: str = "+14155550100",
    organization_id: uuid.UUID | None = None,
    is_pool: bool = False,
) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=organization_id,
        e164=e164,
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id=f"PN-{e164}",
        provisioning_state="active",
        is_pool=is_pool,
    )
    session.add(pn)
    await session.commit()
    await session.refresh(pn)
    return pn


async def _make_pool_number(session, *, e164: str = "+14155550100") -> PhoneNumber:
    return await _make_number(session, e164=e164, is_pool=True)


async def _make_org_number(
    session, *, e164: str, organization_id: uuid.UUID
) -> PhoneNumber:
    return await _make_number(session, e164=e164, organization_id=organization_id)


async def _make_call(session, *, from_number_id: uuid.UUID) -> Call:
    call = Call(
        organization_id=uuid.uuid4(),
        from_number_id=from_number_id,
        from_e164="+14155550100",
        to_e164="+14155551234",
        voice_config={"stt": "deepgram", "tts": "elevenlabs"},
    )
    session.add(call)
    await session.commit()
    await session.refresh(call)
    return call


# ---------------------------------------------------------------------------
# claim_pool_number
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_claim_pool_returns_available_row(async_session):
    pn = await _make_pool_number(async_session)

    claimed = await claim_pool_number(async_session)

    assert claimed is not None
    assert claimed.id == pn.id
    # claim_pool_number does NOT set reserved_call_id — that's the caller's job
    # once they have a Call row to bind to.
    assert claimed.reserved_call_id is None


@pytest.mark.asyncio
async def test_claim_pool_returns_none_when_exhausted(async_session):
    pn = await _make_pool_number(async_session)
    existing_call = await _make_call(async_session, from_number_id=pn.id)
    pn.reserved_call_id = existing_call.id
    await async_session.commit()

    claimed = await claim_pool_number(async_session)
    assert claimed is None


@pytest.mark.asyncio
async def test_claim_pool_skips_quarantined_rows(async_session):
    pn = await _make_pool_number(async_session)
    pn.provisioning_state = "failed"
    await async_session.commit()

    claimed = await claim_pool_number(async_session)
    assert claimed is None


@pytest.mark.asyncio
async def test_claim_pool_ignores_org_owned_numbers(async_session):
    org_id = uuid.uuid4()
    await _make_org_number(async_session, e164="+14155550200", organization_id=org_id)

    claimed = await claim_pool_number(async_session)
    assert claimed is None  # no pool rows; org-owned shouldn't be eligible.


# ---------------------------------------------------------------------------
# release_pool_reservation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_clears_reservation(async_session):
    pn = await _make_pool_number(async_session)
    call = await _make_call(async_session, from_number_id=pn.id)
    pn.reserved_call_id = call.id
    await async_session.commit()

    released = await release_pool_reservation(async_session, call_id=call.id)
    await async_session.commit()

    assert released is True
    await async_session.refresh(pn)
    assert pn.reserved_call_id is None


@pytest.mark.asyncio
async def test_release_is_idempotent(async_session):
    pn = await _make_pool_number(async_session)
    call = await _make_call(async_session, from_number_id=pn.id)
    pn.reserved_call_id = call.id
    await async_session.commit()

    assert await release_pool_reservation(async_session, call_id=call.id) is True
    await async_session.commit()
    # Second call: no row matches — must be a clean no-op, not an error.
    assert await release_pool_reservation(async_session, call_id=call.id) is False


@pytest.mark.asyncio
async def test_release_noop_on_non_pool_call(async_session):
    org_id = uuid.uuid4()
    org_pn = await _make_org_number(
        async_session, e164="+14155550300", organization_id=org_id
    )
    call = await _make_call(async_session, from_number_id=org_pn.id)
    # No reservation ever set — release should match zero rows.
    assert await release_pool_reservation(async_session, call_id=call.id) is False


@pytest.mark.asyncio
async def test_concurrent_claims_pick_different_rows(async_session, database_url):
    """SELECT FOR UPDATE SKIP LOCKED guarantees concurrent claimers don't collide."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from hailhq.core.db import to_async_url

    # Seed two pool rows.
    pn_a = await _make_pool_number(async_session, e164="+14155550400")
    pn_b = await _make_pool_number(async_session, e164="+14155550401")
    await async_session.commit()

    engine = create_async_engine(to_async_url(database_url))
    SessionMaker = async_sessionmaker(engine, expire_on_commit=False)

    async with SessionMaker() as s1, SessionMaker() as s2:
        c1 = await claim_pool_number(s1)
        c2 = await claim_pool_number(s2)
        assert c1 is not None and c2 is not None
        assert c1.id != c2.id  # SKIP LOCKED steered them apart.
        assert {c1.id, c2.id} == {pn_a.id, pn_b.id}
        await s1.commit()
        await s2.commit()

    await engine.dispose()


# ---------------------------------------------------------------------------
# Schema invariant — the CHECK constraint must reject mis-shaped rows.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pool_row_with_org_id_rejected(async_session):
    """CHECK (is_pool ↔ organization_id IS NULL) blocks the inconsistent state."""
    bad = PhoneNumber(
        organization_id=uuid.uuid4(),  # ← inconsistent with is_pool=True
        e164="+14155550500",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id="PN-bad",
        provisioning_state="active",
        is_pool=True,
    )
    async_session.add(bad)
    with pytest.raises(Exception):  # IntegrityError subclass at runtime
        await async_session.commit()
    await async_session.rollback()


@pytest.mark.asyncio
async def test_non_pool_row_without_org_id_rejected(async_session):
    bad = PhoneNumber(
        organization_id=None,  # ← inconsistent with is_pool=False
        e164="+14155550501",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id="PN-bad2",
        provisioning_state="active",
        is_pool=False,
    )
    async_session.add(bad)
    with pytest.raises(Exception):
        await async_session.commit()
    await async_session.rollback()


# ---------------------------------------------------------------------------
# Sweeper — backstop release for stuck reservations
# ---------------------------------------------------------------------------


async def _reserve(session, pn: PhoneNumber, call: Call) -> None:
    pn.reserved_call_id = call.id
    await session.commit()


@pytest.mark.asyncio
async def test_sweeper_releases_terminal_call_reservation(async_session):
    """A pool reservation whose Call is already terminal must be force-released."""
    pn = await _make_pool_number(async_session, e164="+14155550601")
    call = await _make_call(async_session, from_number_id=pn.id)
    await _reserve(async_session, pn, call)

    # Simulate "voicebot wrote terminal status but release helper never fired."
    await async_session.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(status="completed", end_reason="normal_hangup")
    )
    await async_session.commit()

    released = await sweep_pool_reservations(async_session, grace_seconds=120)
    await async_session.commit()

    assert pn.id in released
    await async_session.refresh(pn)
    assert pn.reserved_call_id is None


@pytest.mark.asyncio
async def test_sweeper_releases_after_max_duration_plus_grace(async_session):
    """A call past max_duration_seconds + grace is force-released even if not terminal."""
    pn = await _make_pool_number(async_session, e164="+14155550602")
    call = await _make_call(async_session, from_number_id=pn.id)
    await _reserve(async_session, pn, call)

    # Backdate requested_at far enough that max_duration_seconds + grace has elapsed.
    await async_session.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(
            requested_at=datetime.now(timezone.utc) - timedelta(seconds=600),
            max_duration_seconds=300,
            # Status stays 'queued' — sweeper backstop must fire on time alone.
        )
    )
    await async_session.commit()

    released = await sweep_pool_reservations(async_session, grace_seconds=120)
    await async_session.commit()

    assert pn.id in released
    await async_session.refresh(pn)
    assert pn.reserved_call_id is None


@pytest.mark.asyncio
async def test_sweeper_leaves_live_call_alone(async_session):
    """A reservation tied to a fresh, non-terminal call must NOT be released."""
    pn = await _make_pool_number(async_session, e164="+14155550603")
    call = await _make_call(async_session, from_number_id=pn.id)
    await _reserve(async_session, pn, call)

    await async_session.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(
            status="in_progress",
            requested_at=datetime.now(timezone.utc),
            max_duration_seconds=300,
        )
    )
    await async_session.commit()

    released = await sweep_pool_reservations(async_session, grace_seconds=120)
    await async_session.commit()

    assert pn.id not in released
    await async_session.refresh(pn)
    assert pn.reserved_call_id == call.id


@pytest.mark.asyncio
async def test_sweeper_respects_max_duration_snapshot_not_runtime(async_session):
    """If a call's snapshot max_duration is large, sweeper waits for THAT bound,
    not whatever the current config happens to be."""
    pn = await _make_pool_number(async_session, e164="+14155550604")
    call = await _make_call(async_session, from_number_id=pn.id)
    await _reserve(async_session, pn, call)

    await async_session.execute(
        update(Call)
        .where(Call.id == call.id)
        .values(
            requested_at=datetime.now(timezone.utc) - timedelta(seconds=400),
            max_duration_seconds=1800,  # 30 minutes — still well within bound.
        )
    )
    await async_session.commit()

    released = await sweep_pool_reservations(async_session, grace_seconds=120)
    await async_session.commit()
    assert pn.id not in released


@pytest.mark.asyncio
async def test_sweeper_is_noop_when_nothing_stuck(async_session):
    pn = await _make_pool_number(async_session, e164="+14155550605")
    # No reservation, no call.
    released = await sweep_pool_reservations(async_session, grace_seconds=120)
    assert released == []
    await async_session.refresh(pn)
    assert pn.reserved_call_id is None


@pytest.mark.asyncio
async def test_sweeper_handles_multiple_reservations_in_one_pass(async_session):
    pn_a = await _make_pool_number(async_session, e164="+14155550606")
    pn_b = await _make_pool_number(async_session, e164="+14155550607")
    call_a = await _make_call(async_session, from_number_id=pn_a.id)
    call_b = await _make_call(async_session, from_number_id=pn_b.id)
    await _reserve(async_session, pn_a, call_a)
    await _reserve(async_session, pn_b, call_b)

    await async_session.execute(
        update(Call)
        .where(Call.id.in_([call_a.id, call_b.id]))
        .values(status="completed", end_reason="normal_hangup")
    )
    await async_session.commit()

    released = await sweep_pool_reservations(async_session, grace_seconds=120)
    await async_session.commit()
    assert set(released) == {pn_a.id, pn_b.id}
