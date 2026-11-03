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


# --------------------------------------------------------------------------- #
# Email: hard bounce rate and complaint rate -> ChannelSuspension.
# --------------------------------------------------------------------------- #


async def _seed_emails(
    session, org_id, count: int, *, recipients: int = 1, metadata=None
) -> list:
    from hailhq.core.models import Email, EmailDomain

    domain = EmailDomain(
        organization_id=org_id,
        kind="custom",
        domain=f"{uuid.uuid4().hex[:12]}.example.com",
        verification_status="verified",
        provider="ses",
    )
    session.add(domain)
    await session.flush()
    now = datetime.now(timezone.utc)
    emails = [
        Email(
            organization_id=org_id,
            email_domain_id=domain.id,
            from_address="agent@example.com",
            to_addresses=[f"r{i}-{j}@example.com" for j in range(recipients)],
            subject="hi",
            body_text="hello",
            status="sent",
            sent_at=now,
            metadata_=metadata or {},
        )
        for i in range(count)
    ]
    session.add_all(emails)
    await session.flush()
    return emails


async def _seed_email_events(session, emails, kind: str, **payload) -> None:
    from hailhq.core.models import EmailEvent

    now = datetime.now(timezone.utc)
    for email in emails:
        session.add(
            EmailEvent(
                email_id=email.id,
                organization_id=email.organization_id,
                kind=kind,
                payload={"recipients": [email.to_addresses[0]], **payload},
                occurred_at=now,
            )
        )
    await session.flush()


def _email_thresholds(monkeypatch, *, min_sends: int = 10) -> None:
    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_email_abuse_min_sends", min_sends)
    monkeypatch.setattr(config.settings, "hail_email_abuse_max_bounce_rate", 0.04)
    monkeypatch.setattr(config.settings, "hail_email_abuse_max_complaint_rate", 0.005)


async def _email_suspension(session, org_id):
    from sqlalchemy import select

    return (
        await session.execute(
            select(ChannelSuspension).where(
                ChannelSuspension.organization_id == org_id,
                ChannelSuspension.channel == "email",
            )
        )
    ).scalar_one_or_none()


async def test_email_high_hard_bounce_rate_triggers_suspension(
    async_session, monkeypatch
) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 20)
    await _seed_email_events(async_session, emails[:2], "bounced", hard=True)  # 10%
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1
    row = await _email_suspension(async_session, org_id)
    assert row is not None
    assert "hard bounce rate 10.0%" in row.reason


async def test_email_complaint_rate_triggers_suspension(
    async_session, monkeypatch
) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 100)
    await _seed_email_events(async_session, emails[:1], "complained")  # 1%
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1
    assert "complaint rate" in (await _email_suspension(async_session, org_id)).reason


async def test_email_soft_and_account_list_bounces_do_not_count(
    async_session, monkeypatch
) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 20)
    await _seed_email_events(async_session, emails[:5], "bounced", hard=False)
    await _seed_email_events(
        async_session,
        emails[5:10],
        "bounced",
        hard=True,
        bounce_sub_type="OnAccountSuppressionList",
    )
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 0


async def test_email_rate_counts_every_recipient(async_session, monkeypatch) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    # 10 emails x 10 recipients = 100 recipients; 3 hard bounces = 3%.
    emails = await _seed_emails(async_session, org_id, 10, recipients=10)
    await _seed_email_events(async_session, emails[:3], "bounced", hard=True)
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 0


async def test_email_forwards_do_not_count(async_session, monkeypatch) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    forwards = await _seed_emails(
        async_session, org_id, 20, metadata={"forwarded_from": str(uuid.uuid4())}
    )
    await _seed_email_events(async_session, forwards[:10], "bounced", hard=True)
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 0


async def test_email_low_volume_org_is_not_flagged(async_session, monkeypatch) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch, min_sends=200)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 20)
    await _seed_email_events(async_session, emails[:10], "bounced", hard=True)
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 0


async def test_email_already_suspended_org_is_not_double_suspended(
    async_session, monkeypatch
) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 20)
    await _seed_email_events(async_session, emails[:5], "bounced", hard=True)
    async_session.add(
        ChannelSuspension(organization_id=org_id, channel="email", reason="flagged")
    )
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 0
