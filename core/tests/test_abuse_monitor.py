"""Tests for the SMS abuse-monitoring guardrail: rolling opt-out rate ->
ChannelSuspension."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from hailhq.core.abuse_monitor import check_and_suspend_abusive_orgs
from hailhq.core.models import ChannelSuspension, Suppression, UsageEvent


async def _seed_sends(session, org_id, count: int) -> None:
    now = datetime.now(timezone.utc)
    for i in range(count):
        session.add(
            UsageEvent(
                organization_id=org_id,
                channel="sms",
                units=1,
                ref=f"sms:{i}",
                occurred_at=now,
            )
        )
    await session.flush()


async def _seed_opt_outs(session, org_id, count: int) -> None:
    for i in range(count):
        session.add(
            Suppression(
                organization_id=org_id,
                recipient=f"+1415555{1000+i}",
                channel="sms",
                reason="stop",
                source="stop_keyword",
            )
        )
    await session.flush()


async def test_high_opt_out_rate_triggers_suspension(
    async_session, monkeypatch
) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 20)
    await _seed_opt_outs(async_session, org_id, 5)  # 25% opt-out rate, well over 5%
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)
    await async_session.commit()

    assert suspended_count == 1
    from sqlalchemy import select

    row = (
        await async_session.execute(
            select(ChannelSuspension).where(ChannelSuspension.organization_id == org_id)
        )
    ).scalar_one_or_none()
    assert row is not None
    assert row.channel == "sms"


async def test_low_volume_org_is_not_flagged_even_with_high_rate(
    async_session, monkeypatch
) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 20)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 5)  # below the min_sends floor
    await _seed_opt_outs(async_session, org_id, 3)  # 60% rate, but volume too low
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)

    assert suspended_count == 0


async def test_healthy_org_is_not_flagged(async_session, monkeypatch) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 100)
    await _seed_opt_outs(async_session, org_id, 1)  # 1% rate
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)

    assert suspended_count == 0


async def test_already_suspended_org_is_not_double_suspended(
    async_session, monkeypatch
) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 20)
    await _seed_opt_outs(async_session, org_id, 5)
    async_session.add(
        ChannelSuspension(
            organization_id=org_id, channel="sms", reason="already flagged"
        )
    )
    await async_session.commit()

    suspended_count = await check_and_suspend_abusive_orgs(async_session)

    assert suspended_count == 0
