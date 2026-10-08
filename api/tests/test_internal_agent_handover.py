"""Internal handover routes: route lookup, gating, result recording."""

from __future__ import annotations

import json
import uuid

import pytest
from hailhq.core import hmac_signing
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.config import settings
from hailhq.core.models import (
    Agent,
    AgentHandoverContact,
    Call,
    CallEvent,
    Contact,
    WebhookDelivery,
    WebhookSubscription,
)
from sqlalchemy import select

SECRET = "test-internal-secret"


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, SECRET),
    }


@pytest.fixture(autouse=True)
def _secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_twilio_sip_outbound_trunk_id", "ST_out_tw")


async def _seed(s, add_phone_number, *, phone="+14155550120", link=True):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    contact = Contact(organization_id=org, name="Sam", phone_e164=phone)
    s.add_all([agent, contact])
    await s.flush()
    if link:
        s.add(
            AgentHandoverContact(
                agent_id=agent.id, contact_id=contact.id, note="Billing", position=0
            )
        )
    number = await add_phone_number(s, org)
    call = Call(
        organization_id=org,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164="+14155550199",
        status="in_progress",
        agent_id=agent.id,
        provider="twilio",
        voice_config={},
    )
    s.add(call)
    await s.commit()
    return call, contact


async def _post(client, path, payload):
    body = json.dumps(payload).encode()
    return await client.post(path, content=body, headers=_signed(body))


async def test_handover_returns_route(client, async_session, add_phone_number):
    call, contact = await _seed(async_session, add_phone_number)
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is True
    assert data["to_e164"] == "+14155550120"
    assert data["from_e164"] == call.from_e164
    assert data["trunk_id"] == "ST_out_tw"


@pytest.mark.parametrize("case", ["unlinked", "ended", "suppressed", "unsold", "done"])
async def test_handover_denied(client, async_session, add_phone_number, case):
    call, contact = await _seed(
        async_session,
        add_phone_number,
        link=case != "unlinked",
        phone="+88213000000" if case == "unsold" else "+14155550120",
    )
    if case == "ended":
        call.status = "completed"
        call.end_reason = "normal_hangup"
    if case == "suppressed":
        await add_suppression(
            async_session,
            organization_id=call.organization_id,
            recipient="+14155550120",
            channel="voice",
            reason="manual",
            source="test",
        )
    if case == "done":
        async_session.add(
            CallEvent(call_id=call.id, kind="handover", payload={"outcome": "answered"})
        )
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is False
    assert data["spoken"]
    assert "to_e164" not in data or data["to_e164"] is None


async def test_result_answered_writes_event_and_webhook(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    async_session.add(
        WebhookSubscription(
            organization_id=call.organization_id,
            target_url="https://example.com/hook",
            event_types=["call.transferred"],
            secret_encrypted="x",
        )
    )
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "answered",
            "sip_status": None,
            "ring_ms": 12000,
        },
    )
    assert r.json() == {"ok": True}
    ev = (
        await async_session.execute(
            select(CallEvent).where(CallEvent.call_id == call.id, CallEvent.kind == "handover")
        )
    ).scalar_one()
    assert ev.payload == {
        "contact_id": str(contact.id),
        "name": "Sam",
        "outcome": "answered",
        "sip_status": None,
        "ring_ms": 12000,
    }
    deliveries = (
        await async_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == "call.transferred")
        )
    ).scalars().all()
    assert len(deliveries) == 1


async def test_result_no_answer_writes_event_only(client, async_session, add_phone_number):
    call, contact = await _seed(async_session, add_phone_number)
    await _post(
        client,
        "/internal/agent/handover-result",
        {
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "no_answer",
            "sip_status": 480,
            "ring_ms": 30000,
        },
    )
    deliveries = (
        await async_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == "call.transferred")
        )
    ).scalars().all()
    assert deliveries == []


async def test_result_answered_twice_is_idempotent(client, async_session, add_phone_number):
    call, contact = await _seed(async_session, add_phone_number)
    async_session.add(
        WebhookSubscription(
            organization_id=call.organization_id,
            target_url="https://example.com/hook",
            event_types=["call.transferred"],
            secret_encrypted="x",
        )
    )
    await async_session.commit()
    payload = {
        "call_id": str(call.id),
        "contact_id": str(contact.id),
        "outcome": "answered",
        "sip_status": None,
        "ring_ms": 1000,
    }
    for _ in range(2):
        r = await _post(client, "/internal/agent/handover-result", payload)
        assert r.json() == {"ok": True}
    events = (
        await async_session.execute(
            select(CallEvent).where(CallEvent.call_id == call.id, CallEvent.kind == "handover")
        )
    ).scalars().all()
    deliveries = (
        await async_session.execute(
            select(WebhookDelivery).where(WebhookDelivery.event_type == "call.transferred")
        )
    ).scalars().all()
    assert len(events) == 1
    assert len(deliveries) == 1


async def test_result_unlinked_contact_rejected(client, async_session, add_phone_number):
    call, contact = await _seed(async_session, add_phone_number, link=False)
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "answered",
            "sip_status": None,
            "ring_ms": 1000,
        },
    )
    assert r.json() == {"ok": False}
    events = (
        await async_session.execute(select(CallEvent).where(CallEvent.call_id == call.id))
    ).scalars().all()
    assert events == []


async def test_handover_denied_when_billed_org_has_no_funds(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    call.metadata_ = {CALL_META_BILLED: True}
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is False
    assert data["spoken"]
    assert data.get("to_e164") is None
