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
    assert result.checks["internal_dnc_hit"] is False


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
    assert result.checks["internal_dnc_hit"] is True


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
