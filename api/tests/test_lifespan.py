"""Boot resilience: a deployment without HAIL_WEBHOOK_SECRET_KEY must start."""

from __future__ import annotations

import httpx
import pytest
from hailhq.api.main import app, lifespan
from hailhq.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key


@pytest.mark.asyncio
async def test_lifespan_starts_without_webhook_secret_key(
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,  # pins session_scope() to the test DB
):
    monkeypatch.setattr(settings, "hail_webhook_secret_key", "")
    async with lifespan(app):
        pass  # boot + teardown without raising


@pytest.mark.asyncio
async def test_webhook_routes_503_without_secret_key(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,
):
    monkeypatch.setattr(settings, "hail_webhook_secret_key", "")
    _, _, plain = await insert_org_and_key(async_session)
    resp = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/hook",
            "event_types": ["email.received"],
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
