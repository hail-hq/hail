"""Integration tests for GET /whoami."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from hailhq.api.deps import SELF_HOSTED_ORG_ID
from hailhq.core.config import settings
from hailhq.core.models import User
from sqlalchemy.ext.asyncio import AsyncSession

SHARED_KEY = "hail-test-shared-secret-do-not-use-in-prod"


async def test_whoami_resolves_the_api_key_owner(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    org_id, api_key, plain = org_and_key
    user_id = uuid.UUID(api_key.reference_id)
    async_session.add(
        User(
            id=user_id,
            name="Sarah Chen",
            email="sarah@acme.test",
            created_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    resp = await client.get("/whoami", headers={"Authorization": f"Bearer {plain}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "auth_kind": "apikey",
        "organization_id": str(org_id),
        "user_id": str(user_id),
        "email": "sarah@acme.test",
        "name": "Sarah Chen",
    }


async def test_whoami_without_a_users_row_keeps_the_user_id(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    """A session for a deleted user still answers — id yes, mailbox no."""
    org_id, api_key, plain = org_and_key

    resp = await client.get("/whoami", headers={"Authorization": f"Bearer {plain}"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["organization_id"] == str(org_id)
    assert body["user_id"] == api_key.reference_id
    assert body["email"] is None
    assert body["name"] is None


async def test_whoami_on_the_shared_key_has_no_human(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A self-hosted operator key carries an org, not a person."""
    monkeypatch.setattr(settings, "hail_api_key", SHARED_KEY)

    resp = await client.get(
        "/whoami", headers={"Authorization": f"Bearer {SHARED_KEY}"}
    )

    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "auth_kind": "shared",
        "organization_id": str(SELF_HOSTED_ORG_ID),
        "user_id": None,
        "email": None,
        "name": None,
    }


async def test_whoami_requires_auth(client: httpx.AsyncClient) -> None:
    resp = await client.get("/whoami")
    assert resp.status_code == 401
