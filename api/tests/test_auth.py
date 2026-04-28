"""Tests for the API-key auth flow."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import KEY_SCHEME_PREFIX, generate_key, hash_key, verify_key
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.db import get_session
from hailhq.core.models import ApiKey, Organization


def test_hash_key_roundtrip() -> None:
    plain = KEY_SCHEME_PREFIX + "a" * 43
    _prefix, hex_digest = hash_key(plain)
    assert verify_key(plain, hex_digest) is True
    assert verify_key("wrong", hex_digest) is False


def test_generate_key_format() -> None:
    full, prefix, hex_digest = generate_key()
    assert full.startswith(KEY_SCHEME_PREFIX)
    assert len(prefix) == 8
    assert prefix == full[:8]
    assert len(full) == len(KEY_SCHEME_PREFIX) + 43
    assert verify_key(full, hex_digest) is True


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        principal: Principal = Depends(get_current_principal),
    ) -> dict[str, str]:
        return {
            "api_key_id": str(principal.api_key_id),
            "organization_id": str(principal.organization_id),
        }

    async def override_get_session() -> AsyncIterator[AsyncSession]:
        yield session

    app.dependency_overrides[get_session] = override_get_session
    return app


@pytest.fixture()
async def client(async_session: AsyncSession) -> AsyncIterator[httpx.AsyncClient]:
    app = _build_app(async_session)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


@pytest.fixture()
async def org_and_key(
    async_session: AsyncSession,
) -> tuple[Organization, ApiKey, str]:
    """Create an organization + an api_key with a known plaintext value."""
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


async def test_unauthenticated_request_returns_401(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json().get("detail")


async def test_bad_key_returns_401(client: httpx.AsyncClient) -> None:
    garbage = "hk_thisisnotavalidkeyatall_garbage_garbage_garbage"
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {garbage}"},
    )
    assert resp.status_code == 401
    assert garbage not in resp.text


async def test_valid_key_returns_principal(
    client: httpx.AsyncClient,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    org, api_key, plain = org_and_key
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key_id"] == str(api_key.id)
    assert body["organization_id"] == str(org.id)


async def test_expired_key_returns_403(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    _, api_key, plain = org_and_key
    api_key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await async_session.commit()

    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 403


async def test_last_used_at_is_updated_on_success(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    _, api_key, plain = org_and_key
    assert api_key.last_used_at is None

    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200

    await async_session.refresh(api_key)
    assert api_key.last_used_at is not None
    delta = datetime.now(timezone.utc) - api_key.last_used_at
    assert delta.total_seconds() < 5


async def test_last_used_at_is_throttled(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    """A second request within the throttle window must not re-stamp."""
    _, api_key, plain = org_and_key

    await client.get("/whoami", headers={"Authorization": f"Bearer {plain}"})
    await async_session.refresh(api_key)
    first = api_key.last_used_at

    await client.get("/whoami", headers={"Authorization": f"Bearer {plain}"})
    await async_session.refresh(api_key)
    second = api_key.last_used_at

    assert first is not None
    assert second == first


async def test_wrong_scheme_returns_401(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/whoami",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401
