"""Test fixtures for the API service."""

from __future__ import annotations

import secrets
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import hash_key
from hailhq.api.main import app
from hailhq.api.routes.calls import get_livekit
from hailhq.core.db import get_session
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import (
    AccountCredit,
    ApiKey,
    OrganizationMember,
    PhoneNumber,
)
from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_url,
)


def mint_test_key() -> tuple[str, str]:
    """Generate an auth-backend-shaped plaintext + its storage hash."""
    plain = "hl_live_" + secrets.token_urlsafe(32)
    return plain, hash_key(plain)


async def insert_org_and_key(
    session: AsyncSession,
    *,
    org_name: str = "Acme",  # noqa: ARG001 — kept for backwards-compatible call sites
    org_slug: str = "acme",  # noqa: ARG001
    auth_user_id: str | None = None,
    initial_credit_cents: int = 100_000,
) -> tuple[str, ApiKey, str]:
    """Insert a member + apikey row tied to a synthetic organization id.

    Returns ``(organization_id, api_key, plaintext)``. ``organization_id`` is
    a TEXT sentinel — no row in ``organization`` (that table lives in the
    website's Better Auth migration history). The ``member`` row is what
    ``deps.py`` joins on to resolve ``apikey.referenceId → organizationId``.

    ``initial_credit_cents`` (default $1000) seeds a credit row so the
    balance gate on POST /v1/calls passes. Pass 0 for billing tests that
    specifically want a zero-balance org.
    """
    auth_user_id = auth_user_id or f"user_test_{uuid.uuid4().hex[:12]}"
    organization_id = f"org_test_{uuid.uuid4().hex[:12]}"

    now = datetime.now(timezone.utc)
    session.add(
        OrganizationMember(
            id=f"mem_test_{uuid.uuid4().hex[:12]}",
            user_id=auth_user_id,
            organization_id=organization_id,
            role="owner",
            created_at=now,
        )
    )

    if initial_credit_cents > 0:
        session.add(
            AccountCredit(
                organization_id=organization_id,
                kind="credit",
                channel="credit",
                amount_cents=initial_credit_cents,
                source="test.fixture",
                ref="test.fixture",
            )
        )

    plain, hashed = mint_test_key()
    api_key = ApiKey(
        id=f"apikey_test_{uuid.uuid4().hex[:12]}",
        name="test-key",
        start=plain[:14],
        reference_id=auth_user_id,
        prefix="hl_live_",
        key=hashed,
        created_at=now,
        updated_at=now,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return organization_id, api_key, plain


@pytest.fixture()
def livekit_mock() -> AsyncMock:
    mock = AsyncMock(spec=LiveKitClient)
    mock.create_room.return_value = "hail-test-room"
    mock.dispatch_agent.return_value = "AD_test_dispatch"

    counter = {"n": 0}

    async def _make_participant(**kwargs):
        counter["n"] += 1
        return SimpleNamespace(
            sip_call_id=f"PA_test_sid_{counter['n']}",
            participant_identity=kwargs.get("participant_identity", "caller-test"),
            participant_id=f"PI_test_{counter['n']}",
            room_name=kwargs.get("room_name", "hail-test-room"),
        )

    mock.create_sip_participant.side_effect = _make_participant
    return mock


@pytest.fixture()
async def client(
    async_session: AsyncSession,  # noqa: F811 (re-used as a fixture parameter name)
    livekit_mock: AsyncMock,
) -> AsyncIterator[httpx.AsyncClient]:
    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield async_session

    app.dependency_overrides[get_session] = override_get_session
    app.dependency_overrides[get_livekit] = lambda: livekit_mock

    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_session, None)
        app.dependency_overrides.pop(get_livekit, None)


@pytest.fixture()
async def org_and_key(
    async_session: AsyncSession,  # noqa: F811 (re-used as a fixture parameter name)
) -> tuple[str, ApiKey, str]:
    return await insert_org_and_key(async_session)


@pytest.fixture(autouse=True)
def reset_deps_caches():
    """Clear deps.py process-wide caches between tests."""
    from hailhq.api import deps

    deps.reset_caches()
    yield
    deps.reset_caches()


@pytest.fixture()
def add_phone_number():
    """Factory fixture: ``await add_phone_number(session, org_id, e164=...)``."""

    async def _add(
        session: AsyncSession,
        organization_id: str,
        e164: str = "+14155551234",
        state: str = "active",
        provider_resource_id: str = "PN_test",
    ) -> PhoneNumber:
        pn = PhoneNumber(
            organization_id=organization_id,
            e164=e164,
            country_code="US",
            number_type="local",
            provider_resource_id=provider_resource_id,
            provisioning_state=state,
        )
        session.add(pn)
        await session.commit()
        await session.refresh(pn)
        return pn

    return _add
