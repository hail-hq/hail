"""Tests for the pre-send compliance gate.

Covers: suppression blocks a send (org-scoped and global), premium-rate
prefix blocks, velocity caps, and the ``add_suppression`` helper's
normalization.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core.compliance_gate import (
    add_suppression,
    check_call_allowed,
    check_email_allowed,
)
from hailhq.core.config import settings
from hailhq.core.models import UsageEvent

# --------------------------------------------------------------------------- #
# Suppression list.
# --------------------------------------------------------------------------- #


async def test_check_call_allowed_passes_with_no_suppression(async_session):
    org_id = uuid.uuid4()
    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is True
    assert result.reason is None
    assert result.checks["suppression_hit"] is False


async def test_check_call_allowed_blocks_org_scoped_suppression(async_session):
    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155559999",
        channel="voice",
        reason="recipient_request",
        source="manual",
    )
    await async_session.commit()

    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is False
    assert "suppression list" in result.reason
    assert result.checks["suppression_hit"] is True


async def test_check_call_allowed_does_not_block_other_orgs(async_session):
    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155559999",
        channel="voice",
        reason="recipient_request",
        source="manual",
    )
    await async_session.commit()

    result = await check_call_allowed(async_session, other_org_id, "+14155559999")
    assert result.allowed is True


async def test_check_call_allowed_blocks_global_suppression(async_session):
    """``organization_id=None`` is a platform-wide block — applies to every org."""
    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=None,
        recipient="+14155559999",
        channel="voice",
        reason="known_spam_trap",
        source="manual",
    )
    await async_session.commit()

    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is False


async def test_check_call_allowed_blocks_on_channel_all(async_session):
    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155559999",
        channel="all",
        reason="recipient_request",
        source="manual",
    )
    await async_session.commit()

    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is False


async def test_check_email_allowed_blocks_suppressed_recipient(async_session):
    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="bounced@example.com",
        channel="email",
        reason="bounced",
        source="bounce",
    )
    await async_session.commit()

    result = await check_email_allowed(
        async_session, org_id, ["ok@example.com", "bounced@example.com"]
    )
    assert result.allowed is False
    assert "bounced@example.com" in result.reason
    assert result.checks["suppression_hit"] is True


async def test_add_suppression_normalizes_email_case(async_session):
    org_id = uuid.uuid4()
    row = await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="Alice@Example.COM",
        channel="email",
        reason="recipient_unsubscribed",
        source="unsubscribe_link",
    )
    await async_session.commit()
    assert row.recipient == "alice@example.com"

    result = await check_email_allowed(async_session, org_id, ["alice@example.com"])
    assert result.allowed is False


async def test_check_email_allowed_passes_when_not_suppressed(async_session):
    org_id = uuid.uuid4()
    result = await check_email_allowed(async_session, org_id, ["ok@example.com"])
    assert result.allowed is True
    assert result.checks["suppression_hit"] is False


# --------------------------------------------------------------------------- #
# Premium-rate / high-risk destination prefixes.
# --------------------------------------------------------------------------- #


async def test_check_call_allowed_blocks_premium_rate_prefix(
    async_session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_blocked_e164_prefixes", "+1900,+1976")
    org_id = uuid.uuid4()

    result = await check_call_allowed(async_session, org_id, "+19005551234")
    assert result.allowed is False
    assert "premium-rate" in result.reason
    assert result.checks["premium_rate_blocked"] is True


async def test_check_call_allowed_ignores_unrelated_prefixes(
    async_session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_blocked_e164_prefixes", "+1900")
    org_id = uuid.uuid4()

    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is True


# --------------------------------------------------------------------------- #
# Velocity / new-account caps.
# --------------------------------------------------------------------------- #


async def _seed_usage_events(session, org_id, channel: str, count: int) -> None:
    now = datetime.now(timezone.utc)
    for _ in range(count):
        session.add(
            UsageEvent(
                organization_id=org_id,
                channel=channel,
                units=1,
                occurred_at=now - timedelta(minutes=1),
            )
        )
    await session.commit()


async def test_check_call_allowed_blocks_over_hourly_velocity_cap(
    async_session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_velocity_call_per_hour", 3)
    org_id = uuid.uuid4()
    await _seed_usage_events(async_session, org_id, "voice", 3)

    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is False
    assert "velocity cap exceeded" in result.reason
    assert result.checks["velocity"]["hour_count"] == 3


async def test_check_call_allowed_under_velocity_cap_passes(
    async_session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_velocity_call_per_hour", 5)
    org_id = uuid.uuid4()
    await _seed_usage_events(async_session, org_id, "voice", 2)

    result = await check_call_allowed(async_session, org_id, "+14155559999")
    assert result.allowed is True


async def test_check_email_allowed_blocks_over_daily_velocity_cap(
    async_session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_velocity_email_per_hour", 1000)
    monkeypatch.setattr(settings, "hail_velocity_email_per_day", 2)
    org_id = uuid.uuid4()
    await _seed_usage_events(async_session, org_id, "email", 2)

    result = await check_email_allowed(async_session, org_id, ["ok@example.com"])
    assert result.allowed is False
    assert "velocity cap exceeded" in result.reason


async def test_velocity_cap_is_per_organization(
    async_session, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "hail_velocity_call_per_hour", 1)
    org_id = uuid.uuid4()
    other_org_id = uuid.uuid4()
    await _seed_usage_events(async_session, org_id, "voice", 1)

    result = await check_call_allowed(async_session, other_org_id, "+14155559999")
    assert result.allowed is True


async def test_check_sms_allowed_blocks_suppressed_recipient(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import add_suppression, check_sms_allowed

    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="user opted out",
        source="manual",
    )
    await async_session.commit()

    result = await check_sms_allowed(async_session, org_id, "+14155551234")

    assert result.allowed is False
    assert "suppression" in result.reason.lower()


async def test_check_sms_allowed_permits_clean_recipient(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import check_sms_allowed

    result = await check_sms_allowed(async_session, uuid.uuid4(), "+14155559999")

    assert result.allowed is True
    assert result.checks["suppression_hit"] is False


async def test_check_sms_allowed_blocks_national_dnc_hit(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMS runs the same national-DNC scrub as voice — DNC/TSR rules cover
    marketing texts too."""
    import uuid

    from hailhq.core import compliance_gate
    from hailhq.core.compliance_gate import check_sms_allowed

    monkeypatch.setattr(settings, "hail_national_dnc_enabled", True)

    async def _on_registry(e164: str) -> bool:
        return True

    monkeypatch.setattr(compliance_gate, "check_national_dnc", _on_registry)

    result = await check_sms_allowed(async_session, uuid.uuid4(), "+14155559999")

    assert result.allowed is False
    assert "Do Not Call" in result.reason
    assert result.checks["national_dnc_checked"] is True
    assert result.checks["national_dnc_hit"] is True


async def _seed_sms_attempts(session, org_id, count: int, *, status: str) -> None:
    """Seed ``count`` outbound sms rows (send attempts) for ``org_id``."""
    from hailhq.core.models import PhoneNumber, Sms

    number = PhoneNumber(
        organization_id=org_id,
        e164=f"+1415555{uuid.uuid4().hex[:4]}",
        country_code="US",
        number_type="local",
        provider_resource_id="PNtest",
        provisioning_state="active",
    )
    session.add(number)
    await session.flush()

    now = datetime.now(timezone.utc)
    for _ in range(count):
        session.add(
            Sms(
                organization_id=org_id,
                from_number_id=number.id,
                from_e164=number.e164,
                to_e164="+14155550000",
                direction="outbound",
                status=status,
                body="hi",
                requested_at=now - timedelta(minutes=1),
            )
        )
    await session.commit()


async def test_check_sms_allowed_velocity_counts_failed_attempts(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SMS velocity cap counts attempts (sms rows), not billed usage —
    carrier-rejected sends must still trip it."""
    from hailhq.core.compliance_gate import check_sms_allowed

    monkeypatch.setattr(settings, "hail_velocity_sms_per_hour", 3)
    org_id = uuid.uuid4()
    await _seed_sms_attempts(async_session, org_id, 3, status="failed")

    result = await check_sms_allowed(async_session, org_id, "+14155559999")

    assert result.allowed is False
    assert "velocity cap exceeded" in result.reason
    assert result.checks["velocity"]["hour_count"] == 3


async def test_check_sms_allowed_under_velocity_cap_passes(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.core.compliance_gate import check_sms_allowed

    monkeypatch.setattr(settings, "hail_velocity_sms_per_hour", 5)
    org_id = uuid.uuid4()
    await _seed_sms_attempts(async_session, org_id, 2, status="sent")

    result = await check_sms_allowed(async_session, org_id, "+14155559999")

    assert result.allowed is True


async def test_remove_suppression_deletes_matching_row(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import add_suppression, remove_suppression

    org_id = uuid.uuid4()
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="user opted out",
        source="stop_keyword",
    )
    await async_session.commit()

    removed = await remove_suppression(
        async_session, organization_id=org_id, recipient="+14155551234", channel="sms"
    )
    await async_session.commit()

    assert removed is True

    from hailhq.core.compliance_gate import check_sms_allowed

    result = await check_sms_allowed(async_session, org_id, "+14155551234")
    assert result.allowed is True


async def test_remove_suppression_no_match_returns_false(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import remove_suppression

    removed = await remove_suppression(
        async_session,
        organization_id=uuid.uuid4(),
        recipient="+14155559999",
        channel="sms",
    )
    assert removed is False


async def test_remove_suppression_deletes_all_org_rows_but_spares_platform_row(
    async_session,
) -> None:
    """``suppressions`` has no unique constraint on (recipient, channel,
    organization_id), so an org can accrue more than one row for the same
    recipient — ``remove_suppression`` must delete every org-scoped one (a
    single-row delete would leave the recipient suppressed). It must NOT,
    however, touch a platform-wide (``organization_id IS NULL``) row: those
    are operator-owned global blocks (spam traps, etc.), and letting a
    tenant-authority path delete one would un-suppress that number for every
    org on the platform."""
    import uuid

    from hailhq.core.compliance_gate import (
        add_suppression,
        normalize_recipient,
        remove_suppression,
    )
    from hailhq.core.models import Suppression
    from sqlalchemy import select as sa_select

    org_id = uuid.uuid4()
    recipient = "+14155551234"
    for reason in ("user opted out", "duplicate opt-out"):
        await add_suppression(
            async_session,
            organization_id=org_id,
            recipient=recipient,
            channel="sms",
            reason=reason,
            source="stop_keyword",
        )
    await add_suppression(
        async_session,
        organization_id=None,
        recipient=recipient,
        channel="sms",
        reason="known_spam_trap",
        source="manual",
    )
    await async_session.commit()

    removed = await remove_suppression(
        async_session, organization_id=org_id, recipient=recipient, channel="sms"
    )
    await async_session.commit()

    assert removed is True

    remaining = (
        (
            await async_session.execute(
                sa_select(Suppression).where(
                    Suppression.recipient == normalize_recipient(recipient),
                    Suppression.channel == "sms",
                )
            )
        )
        .scalars()
        .all()
    )
    # Only the platform-wide (NULL-org) row survives; both org rows are gone.
    assert [r.organization_id for r in remaining] == [None]


async def test_check_channel_suspended_blocks_when_suspended(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import check_channel_suspended
    from hailhq.core.models import ChannelSuspension

    org_id = uuid.uuid4()
    async_session.add(
        ChannelSuspension(
            organization_id=org_id, channel="sms", reason="high opt-out rate"
        )
    )
    await async_session.commit()

    assert await check_channel_suspended(async_session, org_id, "sms") is True
    assert await check_channel_suspended(async_session, org_id, "voice") is False


async def test_check_sms_allowed_blocks_when_channel_suspended(async_session) -> None:
    import uuid

    from hailhq.core.compliance_gate import check_sms_allowed
    from hailhq.core.models import ChannelSuspension

    org_id = uuid.uuid4()
    async_session.add(
        ChannelSuspension(organization_id=org_id, channel="sms", reason="abuse")
    )
    await async_session.commit()

    result = await check_sms_allowed(async_session, org_id, "+14155551234")
    assert result.allowed is False
    assert "suspend" in result.reason.lower()
