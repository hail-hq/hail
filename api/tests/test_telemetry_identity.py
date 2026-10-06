from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.api import deps
from hailhq.core.models import Organization, User
from hailhq.core.telemetry_identity import get_identity, identity_scope


@pytest.mark.asyncio
async def test_authenticated_actor_identity_comes_from_database(
    async_session, monkeypatch
):
    org_id, user_id = uuid4(), uuid4()
    from datetime import datetime, timezone

    async_session.add(Organization(id=org_id, name="Example Workspace", origin="human"))
    async_session.add(
        User(
            id=user_id,
            name="Example User",
            email="actor@example.org",
            created_at=datetime.now(timezone.utc),
        )
    )
    await async_session.flush()
    principal = deps.Principal(
        auth_kind="jwt",
        api_key_id=None,
        user_id=user_id,
        organization_id=org_id,
        scopes=["*"],
    )
    monkeypatch.setattr(
        deps, "_resolve_current_principal", AsyncMock(return_value=principal)
    )
    monkeypatch.setattr(deps, "telemetry_enabled", lambda: True)
    with identity_scope({}):
        result = await deps.get_current_principal("Bearer test", async_session)
        assert result is principal
        assert get_identity() == {
            "organization_id": str(org_id),
            "organization_name": "Example Workspace",
            "user_id": str(user_id),
            "user_email": "actor@example.org",
            "actor_kind": "jwt",
            "auth_kind": "jwt",
        }


@pytest.mark.asyncio
async def test_disabled_telemetry_does_not_lookup_identity(monkeypatch):
    principal = deps.Principal(
        auth_kind="shared",
        api_key_id=None,
        user_id=None,
        organization_id=uuid4(),
        scopes=["*"],
    )
    monkeypatch.setattr(
        deps, "_resolve_current_principal", AsyncMock(return_value=principal)
    )
    monkeypatch.setattr(deps, "telemetry_enabled", lambda: False)
    db = SimpleNamespace(execute=AsyncMock())
    assert await deps.get_current_principal("Bearer test", db) is principal
    db.execute.assert_not_awaited()
