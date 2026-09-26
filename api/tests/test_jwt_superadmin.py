import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException
from hailhq.api import deps
from hailhq.core.models import Organization, OrganizationMember


async def _org(session, org_id):
    session.add(Organization(id=org_id, origin="human"))
    await session.commit()


async def _member(session, user_id, org_id):
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


def _patch_jwt(monkeypatch, claims):
    monkeypatch.setattr(deps, "get_jwks_cache", lambda: object())

    async def _fake_verify(*_a, **_k):
        return claims

    monkeypatch.setattr(deps, "verify_jwt", _fake_verify)


async def test_superadmin_claim_opens_a_foreign_org(async_session, monkeypatch):
    user, own, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own)
    await _org(async_session, foreign)
    _patch_jwt(
        monkeypatch,
        {"sub": str(user), "activeOrganizationId": str(foreign), "superadmin": True},
    )
    p = await deps._principal_from_jwt("a.b.c", async_session)
    assert (
        p.organization_id == foreign and p.superadmin is True and p.auth_kind == "jwt"
    )


async def test_superadmin_claim_without_active_org_uses_own_membership(
    async_session, monkeypatch
):
    user, own = uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own)
    _patch_jwt(monkeypatch, {"sub": str(user), "superadmin": True})
    p = await deps._principal_from_jwt("a.b.c", async_session)
    assert p.organization_id == own
    assert p.superadmin is True


async def test_superadmin_claim_unknown_org_is_403(async_session, monkeypatch):
    user = uuid.uuid4()
    _patch_jwt(
        monkeypatch,
        {
            "sub": str(user),
            "activeOrganizationId": str(uuid.uuid4()),
            "superadmin": True,
        },
    )
    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("a.b.c", async_session)
    assert exc.value.status_code == 403


@pytest.mark.parametrize("value", ["true", 1])
async def test_non_boolean_superadmin_claim_is_ignored(
    async_session, monkeypatch, value
):
    user, own, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own)
    await _org(async_session, foreign)
    _patch_jwt(
        monkeypatch,
        {"sub": str(user), "activeOrganizationId": str(foreign), "superadmin": value},
    )
    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("a.b.c", async_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "user is not a member of the requested organization"


async def test_azp_present_ignores_superadmin_claim(async_session, monkeypatch):
    """An OAuth-provider access token (carries ``azp``) never grants
    superadmin, even with ``superadmin: True`` set — the staff role is only
    for first-party console session tokens, which never carry ``azp``."""
    user, own, foreign = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await _member(async_session, user, own)
    await _org(async_session, foreign)

    _patch_jwt(
        monkeypatch,
        {
            "sub": str(user),
            "activeOrganizationId": str(foreign),
            "superadmin": True,
            "azp": "client-x",
        },
    )
    with pytest.raises(HTTPException) as exc:
        await deps._principal_from_jwt("a.b.c", async_session)
    assert exc.value.status_code == 403
    assert exc.value.detail == "user is not a member of the requested organization"

    _patch_jwt(
        monkeypatch,
        {
            "sub": str(user),
            "activeOrganizationId": str(own),
            "superadmin": True,
            "azp": "client-x",
        },
    )
    p = await deps._principal_from_jwt("a.b.c", async_session)
    assert p.superadmin is False


async def test_api_key_principal_is_never_superadmin():
    p = deps.Principal(
        auth_kind="apikey",
        api_key_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        scopes=["*"],
    )
    assert p.superadmin is False
