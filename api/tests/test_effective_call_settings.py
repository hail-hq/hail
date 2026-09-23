from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.api.routes.internal.call_settings import get_call_settings
from hailhq.core.config import settings
from hailhq.core.models import OrganizationCallSettings


@pytest.mark.asyncio
async def test_reads_current_service_default(monkeypatch):
    monkeypatch.setattr(settings, "hail_voice_max_duration_seconds", 420)
    db = AsyncMock()
    db.get.return_value = None
    org = uuid4()
    assert await get_call_settings(org, db) == {
        "max_duration_seconds": 420,
        "default_max_duration_seconds": 420,
        "uses_default": True,
    }
    db.get.assert_awaited_once_with(OrganizationCallSettings, org)


@pytest.mark.asyncio
async def test_workspace_override_wins(monkeypatch):
    monkeypatch.setattr(settings, "hail_voice_max_duration_seconds", 420)
    db = AsyncMock()
    db.get.return_value = SimpleNamespace(max_duration_seconds=120)
    result = await get_call_settings(uuid4(), db)
    assert result["max_duration_seconds"] == 120
    assert result["uses_default"] is False


@pytest.mark.asyncio
async def test_route_requires_internal_signature(monkeypatch):
    import hashlib
    import hmac

    import httpx
    from fastapi import FastAPI
    from hailhq.api.routes.internal.call_settings import router
    from hailhq.core.db import get_session

    monkeypatch.setattr(settings, "hail_internal_secret", "test-secret")
    monkeypatch.setattr(settings, "hail_voice_max_duration_seconds", 420)
    db = AsyncMock()
    db.get.return_value = None

    async def session():
        yield db

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_session] = session
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        path = f"/internal/orgs/{uuid4()}/call-settings"
        assert (await client.get(path)).status_code == 401
        signature = hmac.new(b"test-secret", b"", hashlib.sha256).hexdigest()
        response = await client.get(
            path, headers={"X-Hail-Signature": f"sha256={signature}"}
        )
        assert response.status_code == 200
        assert response.json()["max_duration_seconds"] == 420
    assert not any("call-settings" in path for path in app.openapi()["paths"])
