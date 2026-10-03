"""Agents CRUD and number routing (PATCH /numbers/{id})."""

from __future__ import annotations

import dataclasses
import uuid
from unittest.mock import AsyncMock

import httpx
import pytest
from hailhq.core import carrier_routing
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key


@pytest.fixture()
async def org(async_session: AsyncSession):
    org_id, _key, plain = await insert_org_and_key(async_session)
    return org_id, {"Authorization": f"Bearer {plain}"}


@pytest.fixture()
def inbound_hooks(monkeypatch: pytest.MonkeyPatch) -> dict[str, AsyncMock]:
    """Twilio inbound configured, carrier hooks mocked."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    attach, detach = AsyncMock(), AsyncMock()
    monkeypatch.setitem(
        carrier_routing.CARRIERS,
        "twilio",
        dataclasses.replace(
            carrier_routing.CARRIERS["twilio"],
            attach_inbound=attach,
            detach_inbound=detach,
        ),
    )
    return {"attach": attach, "detach": detach}


async def _seed_number(
    session: AsyncSession, org_id: uuid.UUID, capabilities=("voice", "sms")
) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        capabilities=list(capabilities),
        provider="twilio",
        provider_resource_id="PN_test",
        provisioning_state="active",
    )
    session.add(pn)
    await session.commit()
    await session.refresh(pn)
    return pn


async def _create_agent(client: httpx.AsyncClient, headers, name="Front desk") -> dict:
    r = await client.post(
        "/agents",
        json={
            "name": name,
            "system_prompt": "Book appointments.",
            "first_message": "Hi.",
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    return r.json()


async def test_crud_round_trip(client: httpx.AsyncClient, org) -> None:
    _org_id, headers = org
    created = await _create_agent(client, headers)
    assert created["ai_disclosure"] is True
    assert created["status"] == "live"
    assert created["voice_config"]["tts"] == "cartesia"

    listed = (await client.get("/agents", headers=headers)).json()
    assert [a["id"] for a in listed["items"]] == [created["id"]]

    got = await client.get(f"/agents/{created['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["name"] == "Front desk"

    patched = await client.patch(
        f"/agents/{created['id']}",
        json={
            "first_message": None,
            "status": "paused",
            "ai_disclosure_line": "AI for {org}.",
        },
        headers=headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["first_message"] is None
    assert patched.json()["status"] == "paused"
    assert patched.json()["ai_disclosure_line"] == "AI for {org}."

    deleted = await client.delete(f"/agents/{created['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (
        await client.get(f"/agents/{created['id']}", headers=headers)
    ).status_code == 404


async def test_duplicate_name_conflicts(client: httpx.AsyncClient, org) -> None:
    _org_id, headers = org
    await _create_agent(client, headers)
    r = await client.post(
        "/agents", json={"name": "Front desk", "system_prompt": "x"}, headers=headers
    )
    assert r.status_code == 409


async def test_agents_are_org_scoped(
    client: httpx.AsyncClient, org, async_session: AsyncSession
) -> None:
    _org_id, headers = org
    created = await _create_agent(client, headers)
    _other, _k, other_plain = await insert_org_and_key(
        async_session, org_slug="other", org_name="Other"
    )
    other_headers = {"Authorization": f"Bearer {other_plain}"}
    assert (
        await client.get(f"/agents/{created['id']}", headers=other_headers)
    ).status_code == 404
    assert (await client.get("/agents", headers=other_headers)).json()["items"] == []


async def test_route_number_registers_and_unregisters(
    client: httpx.AsyncClient,
    org,
    async_session: AsyncSession,
    livekit_mock,
    inbound_hooks,
) -> None:
    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)

    r = await client.patch(
        f"/numbers/{number.id}",
        json={"voice_agent_id": agent["id"], "sms_agent_id": agent["id"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["voice_agent_id"] == agent["id"]
    assert body["sms_agent_id"] == agent["id"]
    assert body["inbound_registered"] is True
    inbound_hooks["attach"].assert_awaited_once_with("PN_test", "+14155550100")
    livekit_mock.add_inbound_number.assert_awaited_once_with("ST_in_tw", "+14155550100")

    # Same agent again: nothing re-registered (Review Focus 3).
    r = await client.patch(
        f"/numbers/{number.id}", json={"voice_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 200
    assert inbound_hooks["attach"].await_count == 1
    assert livekit_mock.add_inbound_number.await_count == 1

    # Detach calls only; texts keep their agent.
    r = await client.patch(
        f"/numbers/{number.id}", json={"voice_agent_id": None}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["voice_agent_id"] is None
    assert r.json()["sms_agent_id"] == agent["id"]
    assert r.json()["inbound_registered"] is False
    livekit_mock.remove_inbound_number.assert_awaited_once_with(
        "ST_in_tw", "+14155550100"
    )
    inbound_hooks["detach"].assert_awaited_once()


async def test_route_number_validates_capability_and_agent(
    client: httpx.AsyncClient, org, async_session: AsyncSession, inbound_hooks
) -> None:
    org_id, headers = org
    agent = await _create_agent(client, headers)
    voice_only = await _seed_number(async_session, org_id, capabilities=("voice",))

    r = await client.patch(
        f"/numbers/{voice_only.id}", json={"sms_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/numbers/{voice_only.id}",
        json={"voice_agent_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert r.status_code == 404
    r = await client.patch(f"/numbers/{voice_only.id}", json={}, headers=headers)
    assert r.status_code == 422
    inbound_hooks["attach"].assert_not_awaited()


async def test_route_number_unconfigured_carrier_is_503(
    client: httpx.AsyncClient, org, async_session: AsyncSession, monkeypatch
) -> None:
    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    number_id = number.id
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "")
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "")
    r = await client.patch(
        f"/numbers/{number_id}", json={"voice_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 503
    async_session.expire_all()
    row = await async_session.get(PhoneNumber, number_id)
    assert row.voice_agent_id is None
    assert row.inbound_registered_at is None


async def test_delete_agent_detaches_numbers(
    client: httpx.AsyncClient,
    org,
    async_session: AsyncSession,
    livekit_mock,
    inbound_hooks,
) -> None:
    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    number_id = number.id
    r = await client.patch(
        f"/numbers/{number_id}",
        json={"voice_agent_id": agent["id"], "sms_agent_id": agent["id"]},
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"/agents/{agent['id']}", headers=headers)
    assert r.status_code == 204
    livekit_mock.remove_inbound_number.assert_awaited_once()
    inbound_hooks["detach"].assert_awaited_once()
    async_session.expire_all()
    row = await async_session.get(PhoneNumber, number_id)
    assert row.voice_agent_id is None
    assert row.sms_agent_id is None
    assert row.inbound_registered_at is None


async def test_release_unregisters_first(
    client: httpx.AsyncClient,
    org,
    async_session: AsyncSession,
    livekit_mock,
    inbound_hooks,
    voice_provider_mock,
) -> None:
    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    await client.patch(
        f"/numbers/{number.id}", json={"voice_agent_id": agent["id"]}, headers=headers
    )
    r = await client.delete(f"/numbers/{number.id}", headers=headers)
    assert r.status_code == 204, r.text
    livekit_mock.remove_inbound_number.assert_awaited_once()
    inbound_hooks["detach"].assert_awaited_once()
    voice_provider_mock.release_number.assert_awaited_once_with("PN_test")


async def test_unknown_tools_are_rejected_on_create_and_update(client, org) -> None:
    _org_id, headers = org
    r = await client.post(
        "/agents",
        json={
            "name": "x",
            "system_prompt": "y",
            "tools": ["end_call", "launch_rockets"],
        },
        headers=headers,
    )
    assert r.status_code == 422
    assert "launch_rockets" in r.text
    created = await _create_agent(client, headers)
    r = await client.patch(
        f"/agents/{created['id']}", json={"tools": ["nope"]}, headers=headers
    )
    assert r.status_code == 422
    r = await client.patch(
        f"/agents/{created['id']}", json={"tools": ["end_call"]}, headers=headers
    )
    assert r.status_code == 200
