"""Tests for POST/GET /numbers — generic cross-channel number provisioning."""

from __future__ import annotations

import uuid


async def test_acquire_number_requires_auth(client) -> None:
    resp = await client.post("/numbers", json={"country_code": "US", "number_type": "local"})
    assert resp.status_code == 401


async def test_acquire_number_happy_path(client, org_and_key, voice_provider_mock) -> None:
    _, _, plaintext = org_and_key
    resp = await client.post(
        "/numbers",
        json={"country_code": "US", "number_type": "local"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["e164"]
    assert body["is_dedicated"] is True
    assert "sms" in body["capabilities"] or "voice" in body["capabilities"]


async def test_get_number_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(f"/numbers/{uuid.uuid4()}", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 404


async def test_list_numbers_scoped_to_org(client, async_session, org_and_key) -> None:
    from hailhq.core.models import PhoneNumber

    org_id, _, plaintext = org_and_key
    async_session.add(
        PhoneNumber(
            organization_id=org_id, e164="+14155551111", country_code="US", number_type="local",
            provider_resource_id="PN_a", provisioning_state="active",
        )
    )
    await async_session.commit()

    resp = await client.get("/numbers", headers={"Authorization": f"Bearer {plaintext}"})
    assert resp.status_code == 200
    assert len(resp.json()["items"]) == 1
