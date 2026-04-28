"""Test fixtures for the API service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import AsyncIterator
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import generate_key
from hailhq.api.main import app
from hailhq.api.routes.calls import get_livekit
from hailhq.core.db import get_session
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import ApiKey, Organization, PhoneNumber
from hailhq.core.testing.fixtures import (  # noqa: F401
    async_session,
    database_url,
)


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
) -> tuple[Organization, ApiKey, str]:
    org = Organization(name="Acme", slug="acme")
    async_session.add(org)
    await async_session.flush()

    plain, prefix, hex_digest = generate_key()
    api_key = ApiKey(
        organization_id=org.id,
        name="test-key",
        key_prefix=prefix,
        key_hash=hex_digest,
    )
    async_session.add(api_key)
    await async_session.commit()
    await async_session.refresh(api_key)
    return org, api_key, plain


@pytest.fixture()
def add_phone_number():
    """Factory fixture: ``await add_phone_number(session, org_id, e164=...)``."""

    async def _add(
        session: AsyncSession,
        organization_id,
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
