"""Unit tests for the agent-origin velocity caps (core.agent_caps)."""

from __future__ import annotations

import uuid

import pytest
from hailhq.core.agent_caps import (
    AGENT_OUTBOUND_DISABLED_FLAG,
    check_agent_send_allowed,
)
from hailhq.core.config import settings
from hailhq.core.models import AgentSendLog, Organization, PlatformFlag
from sqlalchemy.ext.asyncio import AsyncSession


async def _mk_org(db: AsyncSession, origin: str) -> uuid.UUID:
    org_id = uuid.uuid4()
    db.add(Organization(id=org_id, origin=origin))
    await db.flush()
    return org_id


async def test_human_org_is_never_capped_or_logged(
    async_session: AsyncSession,
) -> None:
    org = await _mk_org(async_session, "human")
    for _ in range(settings.agent_email_per_hour + 5):
        assert (
            await check_agent_send_allowed(async_session, org, "email", ["a@b.co"])
            is None
        )
    logs = (
        await async_session.execute(
            AgentSendLog.__table__.select().where(AgentSendLog.organization_id == org)
        )
    ).fetchall()
    assert logs == []


async def test_hourly_cap_denies_with_retry_after(async_session: AsyncSession) -> None:
    org = await _mk_org(async_session, "agent")
    for i in range(settings.agent_email_per_hour):
        assert (
            await check_agent_send_allowed(
                async_session, org, "email", [f"r{i % 3}@b.co"]
            )
            is None
        )
    denial = await check_agent_send_allowed(async_session, org, "email", ["r0@b.co"])
    assert denial is not None
    assert "hour" in denial.reason
    assert 0 < denial.retry_after_seconds <= 3600


async def test_multi_recipient_call_denied_when_it_would_breach_hourly_cap(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single email fanned out to many recipients (to+cc+bcc) must count
    every recipient toward the hourly cap, not just one — otherwise cc/bcc
    fan-out defeats the cap in a single call."""
    monkeypatch.setattr(settings, "agent_email_per_hour", 5)
    org = await _mk_org(async_session, "agent")
    # 3 attempts already logged this hour, leaving headroom for 2 more.
    for i in range(3):
        assert (
            await check_agent_send_allowed(async_session, org, "email", [f"r{i}@b.co"])
            is None
        )

    # A single call with 3 recipients would push the hour count to 6 > 5 —
    # must be denied outright (no partial logging).
    denial = await check_agent_send_allowed(
        async_session, org, "email", ["x@b.co", "y@b.co", "z@b.co"]
    )
    assert denial is not None
    assert "hour" in denial.reason

    logs = (
        await async_session.execute(
            AgentSendLog.__table__.select().where(
                AgentSendLog.organization_id == org,
                AgentSendLog.recipient.in_(["x@b.co", "y@b.co", "z@b.co"]),
            )
        )
    ).fetchall()
    assert logs == []

    # A 2-recipient call exactly fills the remaining headroom.
    assert (
        await check_agent_send_allowed(
            async_session, org, "email", ["x@b.co", "y@b.co"]
        )
        is None
    )


async def test_distinct_recipient_cap(async_session: AsyncSession) -> None:
    org = await _mk_org(async_session, "agent")
    for i in range(settings.agent_email_recipients_per_day):
        assert (
            await check_agent_send_allowed(async_session, org, "email", [f"r{i}@b.co"])
            is None
        )
    denial = await check_agent_send_allowed(async_session, org, "email", ["fresh@b.co"])
    assert denial is not None and "recipient" in denial.reason
    # A recipient already contacted today is still allowed (hourly cap permitting).
    assert (
        await check_agent_send_allowed(async_session, org, "email", ["r0@b.co"]) is None
    )


async def test_distinct_recipient_cap_multi_recipient_union(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The distinct-recipient cap is a union check across the whole
    recipient set of one call, not per-recipient — a cc/bcc fan-out past
    the cap in a single call must be denied."""
    monkeypatch.setattr(settings, "agent_email_recipients_per_day", 3)
    org = await _mk_org(async_session, "agent")
    assert (
        await check_agent_send_allowed(
            async_session, org, "email", ["a@b.co", "b@b.co"]
        )
        is None
    )
    # Union of {a,b} + {c,d} = 4 > 3 -> denied.
    denial = await check_agent_send_allowed(
        async_session, org, "email", ["c@b.co", "d@b.co"]
    )
    assert denial is not None and "recipient" in denial.reason
    # Re-sending to already-contacted recipients only is still allowed.
    assert (
        await check_agent_send_allowed(
            async_session, org, "email", ["a@b.co", "b@b.co"]
        )
        is None
    )


async def test_kill_switch_blocks_agent_orgs_only(async_session: AsyncSession) -> None:
    agent_org = await _mk_org(async_session, "agent")
    human_org = await _mk_org(async_session, "human")
    async_session.add(PlatformFlag(key=AGENT_OUTBOUND_DISABLED_FLAG, value="true"))
    await async_session.flush()
    denial = await check_agent_send_allowed(
        async_session, agent_org, "email", ["a@b.co"]
    )
    assert denial is not None and "disabled" in denial.reason
    assert (
        await check_agent_send_allowed(async_session, human_org, "email", ["a@b.co"])
        is None
    )


async def test_global_channel_ceiling(
    async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "agent_global_sms_per_hour", 2)
    org_a = await _mk_org(async_session, "agent")
    org_b = await _mk_org(async_session, "agent")
    assert (
        await check_agent_send_allowed(async_session, org_a, "sms", ["+15550000001"])
        is None
    )
    assert (
        await check_agent_send_allowed(async_session, org_b, "sms", ["+15550000002"])
        is None
    )
    denial = await check_agent_send_allowed(
        async_session, org_a, "sms", ["+15550000003"]
    )
    assert denial is not None and "platform" in denial.reason
