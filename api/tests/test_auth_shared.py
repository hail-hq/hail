"""Tests for the shared-key auth path (self-host mode).

Covers:
* a matching ``HAIL_API_KEY`` yields a Principal with ``api_key_id=None`` and
  the self-hosted sentinel org;
* a non-matching bearer is rejected with 401;
* with no shared key configured and no ``apikey`` table, every request is 401
  (this is the "broken self-host" case — surfaced loudly).
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api import deps as deps_module
from hailhq.api.deps import (
    SELF_HOSTED_ORG_ID,
    Principal,
    get_current_principal,
)
from hailhq.core.config import settings
from hailhq.core.db import get_session

SHARED_KEY = "hail-test-shared-secret-do-not-use-in-prod"


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
def shared_key_set(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure HAIL_API_KEY and yield it; restored automatically on teardown."""
    monkeypatch.setattr(settings, "hail_api_key", SHARED_KEY)
    return SHARED_KEY


async def test_shared_key_returns_principal_with_null_api_key_id(
    client: httpx.AsyncClient,
    shared_key_set: str,
) -> None:
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {shared_key_set}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key_id"] is None
    assert body["organization_id"] == str(SELF_HOSTED_ORG_ID)


async def test_wrong_shared_key_returns_401(
    client: httpx.AsyncClient,
    shared_key_set: str,
) -> None:
    resp = await client.get(
        "/whoami",
        headers={"Authorization": "Bearer not-the-shared-secret"},
    )
    assert resp.status_code == 401


async def test_no_shared_key_no_apikey_returns_401(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No HAIL_API_KEY + apikey table absent → every bearer is rejected."""
    monkeypatch.setattr(settings, "hail_api_key", "")
    # Force the cache to "table absent" so we don't depend on test-DB state.
    monkeypatch.setattr(
        deps_module._caches, "apikey_table_present", False, raising=False
    )

    resp = await client.get(
        "/whoami",
        headers={"Authorization": "Bearer anything"},
    )
    assert resp.status_code == 401


async def test_empty_shared_key_does_not_match_empty_bearer(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defense in depth: an unset shared key must not authenticate an empty token."""
    monkeypatch.setattr(settings, "hail_api_key", "")
    resp = await client.get("/whoami", headers={"Authorization": "Bearer "})
    # Parser rejects empty bearer with 401 before even reaching the check.
    assert resp.status_code == 401
