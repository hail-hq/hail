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
from hailhq.core.models import ApiKey, Organization, PhoneNumber
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
    org_name: str = "Acme",
    org_slug: str = "acme",
    auth_user_id: str | None = None,
) -> tuple[Organization, ApiKey, str]:
    """Insert an org and an apikey row (auth-backend-shaped) tied to it.

    Returns ``(org, api_key, plaintext)``. Tests should use the plaintext as
    the bearer token; the hashed copy lives in ``api_key.key``.
    """
    auth_user_id = auth_user_id or f"user_test_{uuid.uuid4().hex[:12]}"
    org = Organization(name=org_name, slug=org_slug, auth_user_id=auth_user_id)
    session.add(org)
    await session.flush()

    plain, hashed = mint_test_key()
    now = datetime.now(timezone.utc)
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
    await session.refresh(org)
    return org, api_key, plain


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
    return await insert_org_and_key(async_session)


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
