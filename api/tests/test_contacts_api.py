"""Integration tests for the /contacts and /members/{id}/phone routes."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api import deps
from hailhq.core.models import Contact, OrganizationMember, User

from .conftest import insert_org_and_key  # noqa: F401

# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


async def _seed_member(
    session: AsyncSession,
    org_id: uuid.UUID,
    *,
    name: str,
    email: str,
    phone: str | None = None,
    role: str = "member",
) -> uuid.UUID:
    """Insert a users + members row. Mirrors test_contacts_core.py's helper."""
    uid = uuid.uuid4()
    session.add(User(id=uid, name=name, email=email, phone_number=phone))
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            organization_id=org_id,
            user_id=uid,
            role=role,
            created_at=datetime.now(timezone.utc),
        )
    )
    await session.commit()
    return uid


def _configure_jwt_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(deps.settings, "hail_auth_url", "https://issuer.example.com")
    monkeypatch.setattr(
        deps.settings,
        "hail_auth_audiences",
        "https://api.example.com,https://mcp.example.com",
    )


def _install_test_jwks(monkeypatch: pytest.MonkeyPatch, jwks_client_factory) -> None:
    from hailhq.api import auth as _auth

    test_cache = _auth.JWKSCache(
        "https://issuer.example.com/jwks", client_factory=jwks_client_factory
    )
    monkeypatch.setattr(_auth, "_jwks_cache", test_cache)


async def _seed_owner(session: AsyncSession, api_key) -> uuid.UUID:
    """``create_contact`` sets ``created_by = principal.user_id`` (FK ->
    users.id). ``insert_org_and_key`` only backs the api-key owner with an
    OrganizationMember row, not a users row, so any test that POSTs through
    the owner's key needs one seeded or the FK violation on insert gets
    caught by the generic IntegrityError handler and misreported as a 409
    duplicate."""
    uid = uuid.UUID(api_key.reference_id)
    session.add(User(id=uid, name="Owner", email=f"{uid}@acme.com"))
    await session.commit()
    return uid


async def _create_manual(
    client: httpx.AsyncClient, headers: dict, **fields
) -> dict:
    resp = await client.post("/contacts", json=fields, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# GET /contacts
# --------------------------------------------------------------------------- #


async def test_list_union(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    org_id, _api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    uid = await _seed_member(
        async_session, org_id, name="Ada", email="ada@acme.com", phone="+15550001001"
    )
    async_session.add(Contact(organization_id=org_id, name="Maya", email="maya@x.com"))
    await async_session.commit()

    resp = await client.get("/contacts", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 2
    assert items[0]["kind"] == "member"
    assert items[0]["id"] == f"member:{uid}"
    assert items[1]["kind"] == "manual"
    # Manual id is a bare uuid, not member-prefixed.
    uuid.UUID(items[1]["id"])


async def test_list_q_filter(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    org_id, _api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_member(
        async_session, org_id, name="Ada", email="ada@acme.com", phone="+15550001001"
    )
    async_session.add(Contact(organization_id=org_id, name="Maya", email="maya@x.com"))
    await async_session.commit()

    resp = await client.get("/contacts?q=maya", headers=headers)
    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["kind"] == "manual"
    assert items[0]["name"] == "Maya"


# --------------------------------------------------------------------------- #
# POST /contacts
# --------------------------------------------------------------------------- #


async def test_create_manual_phone_only(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    body = await _create_manual(
        client, headers, name="Bob", phone_e164="+14155550100"
    )
    assert body["kind"] == "manual"
    assert body["phone_e164"] == "+14155550100"
    assert body["email"] is None


async def test_create_email_only(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    body = await _create_manual(client, headers, name="Bob", email="bob@x.com")
    assert body["kind"] == "manual"
    assert body["email"] == "bob@x.com"
    assert body["phone_e164"] is None


async def test_create_neither_422(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    resp = await client.post("/contacts", json={"name": "Bob"}, headers=headers)
    assert resp.status_code == 422


async def test_create_duplicate_phone_409(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    await _create_manual(client, headers, name="Bob", phone_e164="+14155550100")
    resp = await client.post(
        "/contacts",
        json={"name": "Bob 2", "phone_e164": "+14155550100"},
        headers=headers,
    )
    assert resp.status_code == 409


async def test_create_duplicate_email_409(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    await _create_manual(client, headers, name="Bob", email="bob@x.com")
    resp = await client.post(
        "/contacts", json={"name": "Bob 2", "email": "bob@x.com"}, headers=headers
    )
    assert resp.status_code == 409


# --------------------------------------------------------------------------- #
# PATCH /contacts/{id}
# --------------------------------------------------------------------------- #


async def test_patch_manual(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    created = await _create_manual(
        client, headers, name="Bob", phone_e164="+14155550100"
    )
    resp = await client.patch(
        f"/contacts/{created['id']}", json={"name": "Bobby"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Bobby"


async def test_patch_member_422(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    uid = await _seed_member(async_session, org_id, name="Ada", email="ada@acme.com")
    resp = await client.patch(
        f"/contacts/member:{uid}", json={"name": "Ada 2"}, headers=headers
    )
    assert resp.status_code == 422
    assert "membership" in str(resp.json()["detail"])


async def test_patch_clear_both_422(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    created = await _create_manual(
        client, headers, name="Bob", phone_e164="+14155550100"
    )
    resp = await client.patch(
        f"/contacts/{created['id']}", json={"phone_e164": None}, headers=headers
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# DELETE /contacts/{id}
# --------------------------------------------------------------------------- #


async def test_delete_manual_204_and_gone(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, api_key, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_owner(async_session, api_key)
    created = await _create_manual(
        client, headers, name="Bob", phone_e164="+14155550100"
    )
    resp = await client.delete(f"/contacts/{created['id']}", headers=headers)
    assert resp.status_code == 204

    # The owner (seeded above so create_contact's created_by FK resolves) is
    # itself a member and stays in the union; only the manual row is gone.
    listed = await client.get("/contacts", headers=headers)
    ids = [item["id"] for item in listed.json()["items"]]
    assert created["id"] not in ids
    assert all(i.startswith("member:") for i in ids)


async def test_delete_member_422(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    uid = await _seed_member(async_session, org_id, name="Ada", email="ada@acme.com")
    resp = await client.delete(f"/contacts/member:{uid}", headers=headers)
    assert resp.status_code == 422


async def test_other_org_contact_404(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, _, plain_a = org_and_key
    headers_a = {"Authorization": f"Bearer {plain_a}"}
    other_org = uuid.uuid4()
    row = Contact(organization_id=other_org, name="OrgB Contact", email="b@x.com")
    async_session.add(row)
    await async_session.commit()
    await async_session.refresh(row)

    patch_resp = await client.patch(
        f"/contacts/{row.id}", json={"name": "Hijacked"}, headers=headers_a
    )
    assert patch_resp.status_code == 404

    delete_resp = await client.delete(f"/contacts/{row.id}", headers=headers_a)
    assert delete_resp.status_code == 404


# --------------------------------------------------------------------------- #
# PUT/DELETE /members/{user_id}/phone
# --------------------------------------------------------------------------- #


async def test_put_own_phone_via_me(
    monkeypatch: pytest.MonkeyPatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _configure_jwt_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    org_id, _, _ = org_and_key
    uid = await _seed_member(async_session, org_id, name="Ada", email="ada@acme.com")
    token = sign_jwt(base_claims(sub=str(uid), aud="https://api.example.com"))

    resp = await client.put(
        "/members/me/phone",
        json={"phone_e164": "+14155550199"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["user_id"] == str(uid)
    assert body["phone_e164"] == "+14155550199"

    row = (
        await async_session.execute(select(User).where(User.id == uid))
    ).scalar_one()
    assert row.phone_number == "+14155550199"


async def test_admin_sets_other_phone(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    org_id, _, plain = org_and_key  # org_and_key seeds caller with role="owner"
    headers = {"Authorization": f"Bearer {plain}"}
    other_uid = await _seed_member(
        async_session, org_id, name="Bob", email="bob@acme.com", role="member"
    )

    resp = await client.put(
        f"/members/{other_uid}/phone",
        json={"phone_e164": "+14155550200"},
        headers=headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["phone_e164"] == "+14155550200"

    row = (
        await async_session.execute(select(User).where(User.id == other_uid))
    ).scalar_one()
    assert row.phone_number == "+14155550200"


async def test_member_cannot_set_other_403(
    monkeypatch: pytest.MonkeyPatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _configure_jwt_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    org_id, _, _ = org_and_key
    caller_uid = await _seed_member(
        async_session, org_id, name="Caller", email="caller@acme.com", role="member"
    )
    other_uid = await _seed_member(
        async_session, org_id, name="Other", email="other@acme.com", role="member"
    )
    token = sign_jwt(base_claims(sub=str(caller_uid), aud="https://api.example.com"))

    resp = await client.put(
        f"/members/{other_uid}/phone",
        json={"phone_e164": "+14155550201"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


async def test_phone_target_not_in_org_404(
    client: httpx.AsyncClient, org_and_key: tuple, async_session: AsyncSession
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    other_org = uuid.uuid4()
    other_org_member = await _seed_member(
        async_session, other_org, name="Stranger", email="stranger@other.com"
    )

    resp = await client.put(
        f"/members/{other_org_member}/phone",
        json={"phone_e164": "+14155550202"},
        headers=headers,
    )
    assert resp.status_code == 404


async def test_delete_phone_clears(
    monkeypatch: pytest.MonkeyPatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _configure_jwt_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    org_id, _, _ = org_and_key
    uid = await _seed_member(
        async_session,
        org_id,
        name="Ada",
        email="ada@acme.com",
        phone="+14155550100",
    )
    token = sign_jwt(base_claims(sub=str(uid), aud="https://api.example.com"))

    resp = await client.delete(
        "/members/me/phone", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 204

    row = (
        await async_session.execute(select(User).where(User.id == uid))
    ).scalar_one()
    assert row.phone_number is None
