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


def _remove_livekit_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``get_livekit_optional`` return None, as on a server with no
    LiveKit settings."""
    from hailhq.api.main import app
    from hailhq.api.routes import calls as calls_routes

    app.dependency_overrides.pop(calls_routes.get_livekit_optional, None)
    monkeypatch.setattr(calls_routes, "_livekit_singleton", None)
    for name in ("livekit_url", "livekit_api_key", "livekit_api_secret"):
        monkeypatch.setattr(settings, name, "")
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(name, raising=False)


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


async def test_patch_null_voice_config_is_ignored(
    client: httpx.AsyncClient, org
) -> None:
    """voice_config is not nullable: a null leaves it alone, never stores JSON
    null (which would break every read of the agent and every inbound call)."""
    _org_id, headers = org
    created = await _create_agent(client, headers)
    r = await client.patch(
        f"/agents/{created['id']}",
        json={"voice_config": None, "status": "paused"},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["voice_config"] == created["voice_config"]
    assert r.json()["status"] == "paused"
    listed = await client.get("/agents", headers=headers)
    assert listed.status_code == 200


async def test_release_works_without_livekit_settings(
    client: httpx.AsyncClient,
    org,
    async_session: AsyncSession,
    voice_provider_mock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server with no LiveKit settings still releases numbers: LiveKit is
    only touched for a number that was registered for inbound calls."""
    _remove_livekit_settings(monkeypatch)

    org_id, headers = org
    number = await _seed_number(async_session, org_id)
    r = await client.delete(f"/numbers/{number.id}", headers=headers)
    assert r.status_code == 204, r.text
    voice_provider_mock.release_number.assert_awaited_once_with("PN_test")


async def test_text_routing_and_agent_delete_work_without_livekit_settings(
    client: httpx.AsyncClient,
    org,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server with no LiveKit settings still routes texts and deletes an
    agent: LiveKit is only touched to change call routing."""
    _remove_livekit_settings(monkeypatch)

    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    number_id = number.id
    r = await client.patch(
        f"/numbers/{number_id}", json={"sms_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["sms_agent_id"] == agent["id"]
    # Clearing the call agent off a number that is not registered is a no-op.
    r = await client.patch(
        f"/numbers/{number_id}", json={"voice_agent_id": None}, headers=headers
    )
    assert r.status_code == 200, r.text

    r = await client.delete(f"/agents/{agent['id']}", headers=headers)
    assert r.status_code == 204, r.text
    async_session.expire_all()
    row = await async_session.get(PhoneNumber, number_id)
    assert row.sms_agent_id is None


async def test_call_routing_without_livekit_settings_is_503(
    client: httpx.AsyncClient,
    org,
    async_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _remove_livekit_settings(monkeypatch)

    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    r = await client.patch(
        f"/numbers/{number.id}", json={"voice_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == "inbound calls are not configured on this server"


async def test_routing_503_does_not_name_the_carrier_or_an_env_var(
    client: httpx.AsyncClient, org, async_session: AsyncSession, monkeypatch
) -> None:
    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "")
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "")
    r = await client.patch(
        f"/numbers/{number.id}", json={"voice_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 503
    detail = r.json()["detail"].lower()
    assert "twilio" not in detail
    assert "livekit_" not in detail


async def test_assigning_texts_agent_sets_sms_up(
    client, org, async_session, inbound_hooks, sms_mock
) -> None:
    """A number bought before automatic setup (no messaging service yet) is
    set up the moment a texts agent is assigned; a second assignment does
    not touch the carrier again."""
    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    assert number.messaging_service_sid is None

    r = await client.patch(
        f"/numbers/{number.id}", json={"sms_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert r.json()["messaging_service_sid"] == "MG_test_service"
    sms_mock.attach_number.assert_awaited_once_with(
        messaging_service_sid="MG_test_service", provider_resource_id="PN_test"
    )

    r = await client.patch(
        f"/numbers/{number.id}", json={"sms_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 200, r.text
    assert sms_mock.attach_number.await_count == 1

    # The repair call sets the service's own settings (inbound webhook)
    # again, so a service made before that setting existed still delivers.
    r = await client.post(f"/numbers/{number.id}/enable-sms", headers=headers)
    assert r.status_code == 200, r.text
    assert sms_mock.ensure_messaging_service.await_count == 2
    assert (
        sms_mock.ensure_messaging_service.await_args.kwargs["existing_sid"]
        == "MG_test_service"
    )
    assert sms_mock.attach_number.await_count == 1


async def test_texts_agent_is_not_assigned_when_sms_setup_fails(
    client, org, async_session, inbound_hooks, sms_mock
) -> None:
    from hailhq.core.providers.sms.base import SmsProvisioningError

    org_id, headers = org
    agent = await _create_agent(client, headers)
    number = await _seed_number(async_session, org_id)
    sms_mock.attach_number.side_effect = SmsProvisioningError("carrier said no")

    r = await client.patch(
        f"/numbers/{number.id}", json={"sms_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 502, r.text
    assert "carrier said no" not in r.text
    await async_session.refresh(number)
    assert number.sms_agent_id is None
    assert number.messaging_service_sid is None


async def test_route_requires_an_agent_that_answers_that_channel(
    client, org, async_session, inbound_hooks
) -> None:
    org_id, headers = org
    r = await client.post(
        "/agents",
        json={"name": "Texts only", "system_prompt": "x", "voice_enabled": False},
        headers=headers,
    )
    assert r.status_code == 201, r.text
    agent = r.json()
    assert agent["voice_enabled"] is False
    number = await _seed_number(async_session, org_id)
    r = await client.patch(
        f"/numbers/{number.id}", json={"voice_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 422
    assert "does not answer calls" in r.text
    r = await client.patch(
        f"/numbers/{number.id}", json={"sms_agent_id": agent["id"]}, headers=headers
    )
    assert r.status_code == 200, r.text


async def test_background_sync_finishes_failed_sms_setup_and_refreshes_services(
    org, async_session: AsyncSession, sms_mock
) -> None:
    """Nobody clicks anything: a number whose SMS setup failed is attached by
    the background pass, and an attached number's service is set again once
    per process."""
    from hailhq.api import number_orders

    org_id, _ = org
    number = await _seed_number(async_session, org_id)
    other = PhoneNumber(
        organization_id=org_id,
        e164="+14155550101",
        country_code="US",
        number_type="local",
        capabilities=["voice", "sms"],
        provider="twilio",
        provider_resource_id="PN_other",
        provisioning_state="active",
        messaging_service_sid="MG_old",
    )
    async_session.add(other)
    await async_session.commit()

    number_orders._sms_services_refreshed = False
    await number_orders.sync_sms_setup()

    await async_session.refresh(number)
    assert number.messaging_service_sid == "MG_test_service"
    sms_mock.attach_number.assert_awaited_once()
    refreshed = [
        c.kwargs["existing_sid"]
        for c in sms_mock.ensure_messaging_service.await_args_list
    ]
    assert "MG_old" in refreshed
    assert number_orders._sms_services_refreshed is True

    # Second pass: nothing left to do, no carrier calls.
    calls = sms_mock.ensure_messaging_service.await_count
    await number_orders.sync_sms_setup()
    assert sms_mock.ensure_messaging_service.await_count == calls


async def test_prompt_templates_show_the_full_prompt_per_channel(client, org) -> None:
    _, headers = org
    r = await client.get("/agents/prompt-templates", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    marker = body["instructions_placeholder"]
    for channel in ("calls_in", "calls_out", "texts"):
        assert body[channel].count(marker) == 1
    assert "answering the call" in body["calls_in"]
    assert "placing the call" in body["calls_out"]
    assert "SMS" in body["texts"]
    # The agent's own text follows Hail's framing, never leads it.
    assert body["calls_in"].index(marker) > body["calls_in"].index("# Guardrails")


async def _contact(client, headers, name="Sam", phone="+14155550111") -> str:
    r = await client.post(
        "/contacts", json={"name": name, "phone_e164": phone}, headers=headers
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_agent_handover_contacts_round_trip(client, org) -> None:
    _, headers = org
    cid = await _contact(client, headers)
    r = await client.post(
        "/agents",
        json={
            "name": "Desk",
            "system_prompt": "Help.",
            "handover_contacts": [{"contact_id": cid, "note": "Billing"}],
        },
        headers=headers,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["handover_contacts"] == [
        {
            "contact_id": cid,
            "name": "Sam",
            "phone_e164": "+14155550111",
            "note": "Billing",
        }
    ]
    agent_id = body["id"]
    r = await client.patch(
        f"/agents/{agent_id}", json={"name": "Desk 2"}, headers=headers
    )
    assert r.json()["handover_contacts"][0]["contact_id"] == cid  # untouched
    r = await client.patch(
        f"/agents/{agent_id}", json={"handover_contacts": []}, headers=headers
    )
    assert r.json()["handover_contacts"] == []
    listed = (await client.get("/agents", headers=headers)).json()["items"]
    assert listed[0]["handover_contacts"] == []


async def test_agent_handover_contact_from_other_org_is_422(client, org) -> None:
    _, headers = org
    r = await client.post(
        "/agents",
        json={
            "name": "Desk",
            "system_prompt": "Help.",
            "handover_contacts": [{"contact_id": str(uuid.uuid4()), "note": "x"}],
        },
        headers=headers,
    )
    assert r.status_code == 422
    assert r.json()["detail"][0]["loc"] == ["body", "handover_contacts", 0]


async def test_agent_handover_note_required(client, org) -> None:
    _, headers = org
    cid = await _contact(client, headers)
    r = await client.post(
        "/agents",
        json={
            "name": "Desk",
            "system_prompt": "Help.",
            "handover_contacts": [{"contact_id": cid, "note": ""}],
        },
        headers=headers,
    )
    assert r.status_code == 422
