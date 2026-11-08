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


async def test_email_repeated_address_counts_once(async_session, monkeypatch) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 20)
    for email in emails:
        # The same address 50 times, in every field: still one recipient.
        address = email.to_addresses[0]
        email.to_addresses = [address] * 50
        email.cc_addresses = [address.upper()]
        email.bcc_addresses = [f" {address} ", f"Bob <{address}>", f"B <{address}> "]
    await _seed_email_events(async_session, emails[:2], "bounced", hard=True)  # 10%
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1


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


async def test_email_late_events_on_old_mail_do_not_count(
    async_session, monkeypatch
) -> None:
    from datetime import timedelta

    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    old = await _seed_emails(async_session, org_id, 10)
    for email in old:
        email.sent_at = datetime.now(timezone.utc) - timedelta(days=30)
    await _seed_emails(async_session, org_id, 20)
    # Complaints arriving now on mail sent 30 days ago.
    await _seed_email_events(async_session, old, "complained")
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 0


async def test_email_repeated_reports_for_one_recipient_count_once(
    async_session, monkeypatch
) -> None:
    from datetime import timedelta

    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs
    from hailhq.core.models import EmailEvent

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 100)
    now = datetime.now(timezone.utc)
    # Five reports for the same recipient: one complaint, 1/100 = 1%.
    for i in range(5):
        async_session.add(
            EmailEvent(
                email_id=emails[0].id,
                organization_id=org_id,
                kind="complained",
                payload={"recipients": [emails[0].to_addresses[0]]},
                occurred_at=now - timedelta(minutes=i),
            )
        )
    # A redacted complaint (no recipients) reported twice on a one-recipient
    # email: capped at the email's one recipient.
    for i in range(2):
        async_session.add(
            EmailEvent(
                email_id=emails[1].id,
                organization_id=org_id,
                kind="complained",
                payload={"recipients": []},
                occurred_at=now - timedelta(minutes=i),
            )
        )
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1
    row = await _email_suspension(async_session, org_id)
    assert "(2/100)" in row.reason


async def test_email_lift_counts_only_mail_sent_after_it(
    async_session, monkeypatch
) -> None:
    from datetime import timedelta

    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    before = await _seed_emails(async_session, org_id, 20)
    for email in before:
        email.sent_at = datetime.now(timezone.utc) - timedelta(days=2)
    await _seed_email_events(async_session, before[:5], "bounced", hard=True)
    async_session.add(
        ChannelSuspension(
            organization_id=org_id,
            channel="email",
            reason="flagged",
            suspended_at=datetime.now(timezone.utc) - timedelta(days=3),
            lifted_at=datetime.now(timezone.utc) - timedelta(days=1),
        )
    )
    after = await _seed_emails(async_session, org_id, 20)
    await async_session.commit()

    # The old bounces were before the lift: no new suspension.
    assert await check_and_suspend_email_orgs(async_session) == 0

    # New bounces after the lift suspend the org again.
    await _seed_email_events(async_session, after[:2], "bounced", hard=True)
    await async_session.commit()
    assert await check_and_suspend_email_orgs(async_session) == 1


async def test_sms_lift_counts_only_activity_after_it(
    async_session, monkeypatch
) -> None:
    from datetime import timedelta

    from hailhq.core import config

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    await _seed_sends(async_session, org_id, 20)
    await _seed_opt_outs(async_session, org_id, 5)
    async_session.add(
        ChannelSuspension(
            organization_id=org_id,
            channel="sms",
            reason="flagged",
            lifted_at=datetime.now(timezone.utc) + timedelta(seconds=1),
        )
    )
    await async_session.commit()

    assert await check_and_suspend_abusive_orgs(async_session) == 0


async def test_tick_runs_email_when_sms_pass_fails(async_session, monkeypatch) -> None:
    from hailhq.core import abuse_monitor

    async def broken(db):
        raise RuntimeError("sms pass failed")

    ran = []

    async def email(db):
        ran.append("email")
        return 1

    monkeypatch.setattr(abuse_monitor, "check_and_suspend_abusive_orgs", broken)
    monkeypatch.setattr(abuse_monitor, "check_and_suspend_email_orgs", email)

    class _Factory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return async_session

        async def __aexit__(self, *exc):
            return False

    worker = abuse_monitor.AbuseMonitorWorker(session_factory=_Factory())
    assert await worker.tick() == 1
    assert ran == ["email"]


async def test_email_redacted_complaints_count_per_report_up_to_recipients(
    async_session, monkeypatch
) -> None:
    from datetime import timedelta

    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs
    from hailhq.core.models import EmailEvent

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    # 1 email to 10 recipients plus 90 single-recipient emails: 100 recipients.
    wide = await _seed_emails(async_session, org_id, 1, recipients=10)
    await _seed_emails(async_session, org_id, 90)
    now = datetime.now(timezone.utc)
    # Three redacted complaints on the 10-recipient email: three people.
    for i in range(3):
        async_session.add(
            EmailEvent(
                email_id=wide[0].id,
                organization_id=org_id,
                kind="complained",
                payload={"recipients": []},
                occurred_at=now - timedelta(minutes=i),
            )
        )
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1
    assert "(3/100)" in (await _email_suspension(async_session, org_id)).reason


async def test_sms_late_stops_on_pre_lift_numbers_do_not_count(
    async_session, monkeypatch
) -> None:
    from datetime import timedelta

    from hailhq.core import config
    from hailhq.core.models import Sms

    monkeypatch.setattr(config.settings, "hail_sms_abuse_min_sends", 10)
    monkeypatch.setattr(config.settings, "hail_sms_abuse_max_opt_out_rate", 0.05)

    org_id = uuid.uuid4()
    async_session.add(
        ChannelSuspension(
            organization_id=org_id,
            channel="sms",
            reason="flagged",
            suspended_at=datetime.now(timezone.utc) - timedelta(days=3),
            lifted_at=datetime.now(timezone.utc) - timedelta(hours=1),
        )
    )
    await _seed_sends(async_session, org_id, 20)  # 20 sends after the lift
    # One number texted after the lift opts out: counts (1/20 = 5%, not over).
    async_session.add(
        Sms(
            organization_id=org_id,
            from_e164="+14155550000",
            to_e164="+14155551000",
            direction="outbound",
            body="hi",
        )
    )
    # 5 STOPs arrive after the lift; only +14155551000 got a post-lift text.
    await _seed_opt_outs(async_session, org_id, 5)
    await async_session.commit()

    assert await check_and_suspend_abusive_orgs(async_session) == 0


async def test_email_cap_uses_distinct_recipients(async_session, monkeypatch) -> None:
    from datetime import timedelta

    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs
    from hailhq.core.models import EmailEvent

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 100)
    address = emails[0].to_addresses[0]
    emails[0].to_addresses = [address, f"Bob <{address.upper()}>"]
    now = datetime.now(timezone.utc)
    # Two redacted complaints on an email with one distinct recipient: one.
    for i in range(2):
        async_session.add(
            EmailEvent(
                email_id=emails[0].id,
                organization_id=org_id,
                kind="complained",
                payload={"recipients": []},
                occurred_at=now - timedelta(minutes=i),
            )
        )
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1
    assert "(1/100)" in (await _email_suspension(async_session, org_id)).reason


async def test_email_reason_names_both_rates_when_both_are_over(
    async_session, monkeypatch
) -> None:
    from hailhq.core.abuse_monitor import check_and_suspend_email_orgs

    _email_thresholds(monkeypatch)
    org_id = uuid.uuid4()
    emails = await _seed_emails(async_session, org_id, 20)
    await _seed_email_events(async_session, emails[:2], "bounced", hard=True)
    await _seed_email_events(async_session, emails[2:3], "complained")
    await async_session.commit()

    assert await check_and_suspend_email_orgs(async_session) == 1
    reason = (await _email_suspension(async_session, org_id)).reason
    assert "hard bounce rate" in reason and "complaint rate" in reason
