"""Tests for the shared-key auth path (self-host mode).

Covers:
* a matching ``HAIL_API_KEY`` resolves to the implicit ``self-hosted`` org;
* a non-matching bearer is rejected with 401;
* the implicit org is lazy-seeded exactly once across multiple requests;
* with no shared key configured and no ``apikey`` table, every request is 401
  (this is the "broken self-host" case — surfaced loudly).
"""

from __future__ import annotations

from typing import AsyncIterator

import httpx
import pytest
from fastapi import Depends, FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api import deps as deps_module
from hailhq.api.deps import Principal, get_current_principal
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import Organization

SHARED_KEY = "hail-test-shared-secret-do-not-use-in-prod"


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()

    @app.get("/whoami")
    async def whoami(
        principal: Principal = Depends(get_current_principal),
    ) -> dict[str, str]:
        return {
            "api_key_id": principal.api_key_id,
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


@pytest.fixture(autouse=True)
def _reset_module_cache(monkeypatch: pytest.MonkeyPatch):
    """Clear the per-process caches between tests."""
    monkeypatch.setattr(deps_module, "_apikey_table_present", None, raising=False)
    monkeypatch.setattr(deps_module, "_self_hosted_org_id", None, raising=False)


@pytest.fixture()
def shared_key_set(monkeypatch: pytest.MonkeyPatch) -> str:
    """Configure HAIL_API_KEY and yield it; restored automatically on teardown."""
    monkeypatch.setattr(settings, "hail_api_key", SHARED_KEY)
    return SHARED_KEY


async def test_shared_key_returns_implicit_org_principal(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    shared_key_set: str,
) -> None:
    resp = await client.get(
        "/whoami",
        headers={"Authorization": f"Bearer {shared_key_set}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["api_key_id"] == "shared"

    # The principal points at the lazy-seeded org.
    org = (
        await async_session.execute(
            select(Organization).where(Organization.slug == "self-hosted")
        )
    ).scalar_one()
    assert body["organization_id"] == str(org.id)
    assert org.name == "Self-hosted"


async def test_wrong_shared_key_returns_401(
    client: httpx.AsyncClient,
    shared_key_set: str,
) -> None:
    resp = await client.get(
        "/whoami",
        headers={"Authorization": "Bearer not-the-shared-secret"},
    )
    assert resp.status_code == 401


async def test_implicit_org_is_seeded_only_once(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    shared_key_set: str,
) -> None:
    for _ in range(3):
        resp = await client.get(
            "/whoami",
            headers={"Authorization": f"Bearer {shared_key_set}"},
        )
        assert resp.status_code == 200

    rows = (
        (
            await async_session.execute(
                select(Organization).where(Organization.slug == "self-hosted")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_no_shared_key_no_apikey_returns_401(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No HAIL_API_KEY + apikey table absent → every bearer is rejected."""
    monkeypatch.setattr(settings, "hail_api_key", "")
    # Force the cache to "table absent" so we don't depend on test-DB state.
    monkeypatch.setattr(deps_module, "_apikey_table_present", False, raising=False)

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
