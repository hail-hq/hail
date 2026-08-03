"""B: the JWT principal path honors the ``activeOrganizationId`` claim.

A console token carries the session's *selected* org. We must resolve a
request to that org (validated against membership), not an arbitrary one —
otherwise a multi-org user's request can land in the wrong tenant.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from hailhq.api import deps
from hailhq.core.models import OrganizationMember


async def _member(session, user_id: uuid.UUID, org_id: uuid.UUID) -> None:
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user_id,
            organization_id=org_id,
            role="owner",
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()


def _patch_jwt(monkeypatch, claims: dict) -> None:
    monkeypatch.setattr(deps, "get_jwks_cache", lambda: object())

    async def _fake_verify(*_a, **_k):
        return claims

    monkeypatch.setattr(deps, "verify_jwt", _fake_verify)


async def test_jwt_resolves_to_active_org_claim(async_session, monkeypatch):
    user, org_a, org_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, org_a)
    await _member(async_session, user, org_b)
    _patch_jwt(monkeypatch, {"sub": str(user), "activeOrganizationId": str(org_b)})

    principal = await deps._principal_from_jwt("a.b.c", async_session)
    assert principal.organization_id == org_b  # the *selected* org, not arbitrary


async def test_jwt_rejects_active_org_the_user_is_not_in(async_session, monkeypatch):
    user, org_a, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, org_a)
    _patch_jwt(monkeypatch, {"sub": str(user), "activeOrganizationId": str(foreign)})

    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("a.b.c", async_session)
    assert exc.value.status_code == 403


async def test_jwt_multi_org_without_claim_does_not_crash(async_session, monkeypatch):
    # Pre-fix this raised MultipleResultsFound; now it picks one deterministically.
    user, org_a, org_b = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, org_a)
    await _member(async_session, user, org_b)
    _patch_jwt(monkeypatch, {"sub": str(user)})

    principal = await deps._principal_from_jwt("a.b.c", async_session)
    assert principal.organization_id in (org_a, org_b)
