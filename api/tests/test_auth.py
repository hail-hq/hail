"""Tests for the API-key auth flow.

hail/api consumes the auth backend's ``apikey`` table directly. These tests
verify:
* the storage hash matches the backend's format (sha256 → base64url, no
  padding);
* unauthenticated / wrong-scheme / bad-key requests are rejected;
* a valid bearer resolves to its Organization via the ``member`` join;
* a valid bearer with no ``member`` row gets a 403 ("not provisioned");
* expired and disabled keys are rejected;
* ``lastRequest`` is stamped on the apikey row but throttled to once per
  minute per key.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import hash_key
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.db import get_session
from hailhq.core.models import ApiKey


def test_hash_key_format() -> None:
    """The hash is base64url(sha256(plain)) without padding."""
    plain = "hl_live_known-test-token"
    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(plain.encode()).digest())
        .rstrip(b"=")
        .decode("ascii")
    )
    assert hash_key(plain) == expected
    # Sanity check: the no-padding invariant holds for an empty string too.
    assert "=" not in hash_key("")


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        principal: Principal = Depends(get_current_principal),
    ) -> dict[str, str | None]:
        return {
            "api_key_id": (
                str(principal.api_key_id) if principal.api_key_id is not None else None
            ),
            "organization_id": str(principal.organization_id),
            "user_id": (
                str(principal.user_id) if principal.user_id is not None else None
            ),
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


async def test_unauthenticated_request_returns_401(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/whoami")
    assert resp.status_code == 401
    assert resp.json().get("detail")


async def test_bad_key_returns_401(client: httpx.AsyncClient) -> None:
    garbage = "hl_live_thisisnotavalidkeyatall_garbage_garbage_garbage"
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {garbage}"},
    )
    assert resp.status_code == 401
    assert garbage not in resp.text


async def test_valid_key_returns_principal(
    client: httpx.AsyncClient,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    org_id, api_key, plain = org_and_key
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key_id"] == str(api_key.id)
    assert body["organization_id"] == str(org_id)


async def test_api_key_principal_carries_user_id(
    client: httpx.AsyncClient,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    """The api-key principal's user_id is the key owner's user uuid.

    ``api_keys.reference_id`` (opaque TEXT upstream) is the auth backend's
    user id, minted as a UUID string by ``insert_org_and_key`` — the same
    value the members join already casts to UUID to resolve the org.
    """
    _, api_key, plain = org_and_key
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["user_id"] == str(uuid.UUID(api_key.reference_id))


async def test_expired_key_returns_403(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    _, api_key, plain = org_and_key
    api_key.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await async_session.commit()

    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 403


async def test_disabled_key_returns_401(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    _, api_key, plain = org_and_key
    api_key.enabled = False
    await async_session.commit()

    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 401


async def test_last_request_is_updated_on_success(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    _, api_key, plain = org_and_key
    assert api_key.last_request is None

    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200

    await async_session.refresh(api_key)
    assert api_key.last_request is not None
    delta = datetime.now(timezone.utc) - api_key.last_request
    assert delta.total_seconds() < 5


async def test_last_request_is_throttled(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    """A second request within the throttle window must not re-stamp."""
    _, api_key, plain = org_and_key

    await client.get("/whoami", headers={"Authorization": f"Bearer {plain}"})
    await async_session.refresh(api_key)
    first = api_key.last_request

    await client.get("/whoami", headers={"Authorization": f"Bearer {plain}"})
    await async_session.refresh(api_key)
    second = api_key.last_request

    assert first is not None
    assert second == first


async def test_wrong_scheme_returns_401(client: httpx.AsyncClient) -> None:
    resp = await client.get(
        "/whoami",
        headers={"Authorization": "Basic dXNlcjpwYXNz"},
    )
    assert resp.status_code == 401


async def test_unprovisioned_user_returns_403(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
) -> None:
    """A valid key whose referenceId has no ``member`` row gets 403, not a fabricated org."""
    plain = "hl_live_orphan-key-no-org"
    reference_id = str(uuid.uuid4())
    now = datetime.now(timezone.utc)
    api_key = ApiKey(
        id=uuid.uuid4(),
        name="orphan",
        start=plain[:14],
        reference_id=reference_id,
        prefix="hl_live_",
        key=hash_key(plain),
        created_at=now,
        updated_at=now,
    )
    async_session.add(api_key)
    await async_session.commit()

    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 403
    assert "not provisioned" in resp.json()["detail"].lower()
