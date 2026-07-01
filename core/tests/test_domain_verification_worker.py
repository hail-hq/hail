from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from hailhq.core.domain_verification_worker import DomainVerificationWorker
from hailhq.core.models import EmailDomain
from hailhq.core.providers.email.base import ProviderIdentity


async def _insert_pending(session_factory, *, age_hours: float = 0.0) -> uuid.UUID:
    async with session_factory() as s:
        sd = EmailDomain(
            organization_id=uuid.uuid4(),
            kind="custom",
            domain="acme.com",
            verification_status="pending",
            dns_records=[],
            provider="ses",
            created_at=datetime.now(timezone.utc) - timedelta(hours=age_hours),
        )
        s.add(sd)
        await s.commit()
        await s.refresh(sd)
        return sd.id


@pytest.mark.usefixtures("db")
async def test_tick_flips_pending_to_verified(session_factory) -> None:
    sd_id = await _insert_pending(session_factory)
    provider = AsyncMock()
    provider.get_identity.return_value = ProviderIdentity(
        domain="acme.com",
        verification_status="verified",
        dkim_records=[],
        mail_from_domain="send.acme.com",
        mail_from_status="verified",
    )
    worker = DomainVerificationWorker(
        session_factory=session_factory,
        provider_factory=lambda: provider,
    )
    processed = await worker.tick()
    assert processed == 1
    async with session_factory() as s:
        sd = (
            await s.execute(select(EmailDomain).where(EmailDomain.id == sd_id))
        ).scalar_one()
    assert sd.verification_status == "verified"
    assert sd.verified_at is not None


@pytest.mark.usefixtures("db")
async def test_tick_verified_custom_enables_inbound_and_adds_receive_mx(
    session_factory,
) -> None:
    """Background verify must match the click-verify endpoint: turn receiving
    on and include the SES inbound-receipt MX in the record list."""
    sd_id = await _insert_pending(session_factory)
    provider = AsyncMock()
    provider.get_identity.return_value = ProviderIdentity(
        domain="acme.com",
        verification_status="verified",
        dkim_records=[],
        mail_from_domain="send.acme.com",
        mail_from_status="verified",
    )
    worker = DomainVerificationWorker(
        session_factory=session_factory,
        provider_factory=lambda: provider,
    )
    await worker.tick()
    async with session_factory() as s:
        sd = (
            await s.execute(select(EmailDomain).where(EmailDomain.id == sd_id))
        ).scalar_one()
    assert sd.inbound_enabled is True
    mx = [
        r
        for r in sd.dns_records
        if r.get("type") == "MX" and r.get("name") == "acme.com"
    ]
    assert mx, sd.dns_records


@pytest.mark.usefixtures("db")
async def test_tick_fails_stale_pending_past_ttl(session_factory) -> None:
    sd_id = await _insert_pending(session_factory, age_hours=80)  # > 72h
    provider = AsyncMock()
    provider.get_identity.return_value = ProviderIdentity(
        domain="acme.com",
        verification_status="pending",
        dkim_records=[],
    )
    worker = DomainVerificationWorker(
        session_factory=session_factory,
        provider_factory=lambda: provider,
        verify_ttl_seconds=72 * 3600,
    )
    await worker.tick()
    async with session_factory() as s:
        sd = (
            await s.execute(select(EmailDomain).where(EmailDomain.id == sd_id))
        ).scalar_one()
    assert sd.verification_status == "failed"
