import uuid
from datetime import datetime, timezone

import pytest
from hailhq.core.forward_limiter import ForwardLimiter
from hailhq.core.models import Email, EmailDomain


async def _seed_domain(
    session,
    org_id: uuid.UUID,
    *,
    suffix: str = "acme",
) -> EmailDomain:
    domain = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain=f"alice+{suffix}@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org=suffix,
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    session.add(domain)
    await session.commit()
    return domain


async def _seed_forwarded_row(
    session,
    *,
    org_id: uuid.UUID,
    domain_id: uuid.UUID,
) -> None:
    session.add(
        Email(
            organization_id=org_id,
            email_domain_id=domain_id,
            from_address="forwarder+acme@mail.hail.so",
            to_addresses=["x@example.com"],
            subject="Fwd: t",
            body_text="x",
            status="sent",
            provider="ses",
            direction="outbound",
            metadata_={"forwarded_from": str(uuid.uuid4())},
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_under_cap_allows(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
    limiter = ForwardLimiter(default_per_hour=10)
    assert (
        await limiter.can_forward(
            async_session,
            organization_id=org_id,
            email_domain_id=domain.id,
            override=None,
        )
        is True
    )


@pytest.mark.asyncio
async def test_at_cap_denies(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
    for _ in range(10):
        async_session.add(
            Email(
                organization_id=org_id,
                email_domain_id=domain.id,
                from_address="forwarder+acme@mail.hail.so",
                to_addresses=["x@example.com"],
                subject="Fwd: t",
                body_text="x",
                status="sent",
                provider="ses",
                direction="outbound",
                metadata_={"forwarded_from": str(uuid.uuid4())},
            )
        )
    await async_session.commit()
    limiter = ForwardLimiter(default_per_hour=10)
    assert (
        await limiter.can_forward(
            async_session,
            organization_id=org_id,
            email_domain_id=domain.id,
            override=None,
        )
        is False
    )


@pytest.mark.asyncio
async def test_override_overrides_default(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
    # 3 forwarded rows; default cap is 10 but per-domain override is 2.
    for _ in range(3):
        async_session.add(
            Email(
                organization_id=org_id,
                email_domain_id=domain.id,
                from_address="forwarder+acme@mail.hail.so",
                to_addresses=["x@example.com"],
                subject="Fwd: t",
                body_text="x",
                status="sent",
                provider="ses",
                direction="outbound",
                metadata_={"forwarded_from": str(uuid.uuid4())},
            )
        )
    await async_session.commit()
    limiter = ForwardLimiter(default_per_hour=10)
    assert (
        await limiter.can_forward(
            async_session,
            organization_id=org_id,
            email_domain_id=domain.id,
            override=2,
        )
        is False
    )


@pytest.mark.asyncio
async def test_non_forward_outbound_rows_dont_count(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
    # 5 ordinary outbound emails (no forwarded_from in metadata)
    for _ in range(5):
        async_session.add(
            Email(
                organization_id=org_id,
                email_domain_id=domain.id,
                from_address="forwarder+acme@mail.hail.so",
                to_addresses=["x@example.com"],
                subject="t",
                body_text="x",
                status="sent",
                provider="ses",
                direction="outbound",
                metadata_={},
            )
        )
    await async_session.commit()
    limiter = ForwardLimiter(default_per_hour=2)
    assert (
        await limiter.can_forward(
            async_session,
            organization_id=org_id,
            email_domain_id=domain.id,
            override=None,
        )
        is True
    )


@pytest.mark.asyncio
async def test_zero_cap_denies(async_session):
    org_id = uuid.uuid4()
    domain = await _seed_domain(async_session, org_id)
    limiter = ForwardLimiter(default_per_hour=0)
    assert (
        await limiter.can_forward(
            async_session,
            organization_id=org_id,
            email_domain_id=domain.id,
            override=None,
        )
        is False
    )


@pytest.mark.asyncio
async def test_cap_is_scoped_per_domain(async_session):
    """A forward against domain A doesn't consume domain B's budget.

    Both domains belong to the same org — that's the key invariant. If the
    filter were org-scoped only, seeding a row for domain A would exhaust
    domain B's budget too.
    """
    org_id = uuid.uuid4()
    dom_a = await _seed_domain(async_session, org_id, suffix="alpha")
    dom_b = await _seed_domain(async_session, org_id, suffix="beta")
    limiter = ForwardLimiter(default_per_hour=1)

    # Seed one forwarded outbound row attributed to domain A.
    await _seed_forwarded_row(async_session, org_id=org_id, domain_id=dom_a.id)

    # Domain A is now at its cap of 1...
    assert not await limiter.can_forward(
        async_session,
        organization_id=dom_a.organization_id,
        email_domain_id=dom_a.id,
        override=1,
    )
    # ...but domain B still has its full budget (same org, different domain).
    assert await limiter.can_forward(
        async_session,
        organization_id=dom_b.organization_id,
        email_domain_id=dom_b.id,
        override=1,
    )
