"""Internal handover routes: route lookup, gating, result recording."""

from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock

import pytest
from hailhq.core import hmac_signing
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.compliance_gate import GateResult, add_suppression
from hailhq.core.config import settings
from hailhq.core.handover import ON_CALL_LINE
from hailhq.core.models import (
    AccountCredit,
    Agent,
    AgentHandoverContact,
    AuditLog,
    Call,
    CallEvent,
    Contact,
    Email,
    EmailDomain,
    PhoneNumber,
    SipConnection,
    Sms,
    UsageEvent,
    WebhookDelivery,
    WebhookSubscription,
)
from sqlalchemy import select

from .conftest import approve_us_sms

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


async def test_handover_to_country_another_carrier_sells(
    client, async_session, add_phone_number
):
    # Twilio sells no voice number in Sweden; Telnyx and DIDWW do.
    call, contact = await _seed(async_session, add_phone_number, phone="+46762000000")
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is True
    assert data["to_e164"] == "+46762000000"
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


@pytest.mark.parametrize("direction", ["inbound", "outbound"])
async def test_handover_denied_when_contact_is_on_the_call(
    client, async_session, add_phone_number, direction
):
    # The person to hand over to is the other party: dialing them rings the
    # phone already on this call.
    call, contact = await _seed(async_session, add_phone_number)
    call.direction = direction
    if direction == "inbound":
        call.to_number_id, call.from_number_id = call.from_number_id, None
        call.to_e164, call.from_e164 = call.from_e164, contact.phone_e164
    else:
        call.to_e164 = contact.phone_e164
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    data = r.json()
    assert data["ok"] is False
    assert data["spoken"] == (
        "It seems this call is already on Sam's number. Handing it over to the "
        "same number is not technically possible."
    )
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.resource_id == call.id)
        )
    ).scalar_one()
    assert audit.payload["reason"] == "contact_is_on_call"


async def test_result_answered_writes_event_and_webhook(
    client, async_session, add_phone_number
):
    """An older voicebot still sends ``picked_up_by``: accepted, not stored."""
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
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "answered",
            "sip_status": None,
            "ring_ms": 12000,
            "picked_up_by": "silent",
            "sms": "blocked:us_registration",
        },
    )
    assert r.json() == {"ok": True}
    ev = (
        await async_session.execute(
            select(CallEvent).where(
                CallEvent.call_id == call.id, CallEvent.kind == "handover"
            )
        )
    ).scalar_one()
    assert ev.payload == {
        "tool_invocation_id": ev.payload["tool_invocation_id"],
        "contact_id": str(contact.id),
        "name": "Sam",
        "outcome": "answered",
        "sip_status": None,
        "ring_ms": 12000,
        "sms": "blocked:us_registration",
        "email": None,
    }
    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "call.transferred"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) == 1


async def test_result_no_answer_writes_event_only(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    await _post(
        client,
        "/internal/agent/handover-result",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "no_answer",
            "sip_status": 480,
            "ring_ms": 30000,
        },
    )
    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "call.transferred"
                )
            )
        )
        .scalars()
        .all()
    )
    assert deliveries == []


async def test_result_answered_twice_is_idempotent(
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
    payload = {
        "tool_invocation_id": str(uuid.uuid4()),
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
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call.id, CallEvent.kind == "handover"
                )
            )
        )
        .scalars()
        .all()
    )
    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "call.transferred"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert len(deliveries) == 1


async def test_result_unlinked_contact_rejected(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number, link=False)
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "answered",
            "sip_status": None,
            "ring_ms": 1000,
        },
    )
    assert r.json() == {"ok": False}
    events = (
        (
            await async_session.execute(
                select(CallEvent).where(CallEvent.call_id == call.id)
            )
        )
        .scalars()
        .all()
    )
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
    rows = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "agent.handover.blocked")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].payload["reason"] == "insufficient_funds"


async def test_result_retry_with_same_invocation_id_is_recorded_once(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    payload = {
        "tool_invocation_id": str(uuid.uuid4()),
        "call_id": str(call.id),
        "contact_id": str(contact.id),
        "outcome": "no_answer",
        "sip_status": 480,
        "ring_ms": 30000,
    }
    for _ in range(2):
        r = await _post(client, "/internal/agent/handover-result", payload)
        assert r.json() == {"ok": True}
    # A second, separate attempt is a new event.
    await _post(
        client,
        "/internal/agent/handover-result",
        {**payload, "tool_invocation_id": str(uuid.uuid4())},
    )
    events = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call.id, CallEvent.kind == "handover"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 2


async def test_handover_ignores_the_org_velocity_cap(
    client, async_session, add_phone_number, monkeypatch
):
    """A transfer joins a call that was already counted; the cap must not
    refuse it."""
    monkeypatch.setattr(settings, "hail_velocity_call_per_hour", 1)
    call, contact = await _seed(async_session, add_phone_number)
    async_session.add(
        UsageEvent(organization_id=call.organization_id, channel="voice", units=60)
    )
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    assert r.json()["ok"] is True


async def _seed_member(s, add_phone_number, *, in_org=True, phone="+14155550150"):
    """A call whose agent links a team member (``member:<user id>``)."""
    from datetime import datetime, timezone

    from hailhq.core.models import OrganizationMember, User

    call, _ = await _seed(s, add_phone_number, link=False)
    user = User(
        id=uuid.uuid4(),
        name="Ana",
        email=f"{uuid.uuid4().hex}@example.com",
        phone_number=phone,
        created_at=datetime.now(timezone.utc),
    )
    s.add(user)
    if in_org:
        s.add(
            OrganizationMember(
                id=uuid.uuid4(),
                user_id=user.id,
                organization_id=call.organization_id,
                role="member",
                created_at=datetime.now(timezone.utc),
            )
        )
    s.add(
        AgentHandoverContact(
            agent_id=call.agent_id, user_id=user.id, note="Sales", position=0
        )
    )
    await s.commit()
    return call, f"member:{user.id}"


async def test_handover_to_team_member_returns_route(
    client, async_session, add_phone_number
):
    call, wire = await _seed_member(async_session, add_phone_number)
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": wire},
    )
    data = r.json()
    assert data["ok"] is True
    assert data["to_e164"] == "+14155550150"


@pytest.mark.parametrize("case", ["left_org", "unlinked", "malformed"])
async def test_handover_to_team_member_denied(
    client, async_session, add_phone_number, case
):
    call, wire = await _seed_member(
        async_session, add_phone_number, in_org=case != "left_org"
    )
    if case == "unlinked":
        wire = f"member:{uuid.uuid4()}"
    if case == "malformed":
        wire = "member:nope"
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": wire},
    )
    assert r.status_code in (200, 422)
    if r.status_code == 200:
        assert r.json()["ok"] is False


async def test_result_for_team_member_carries_wire_id(
    client, async_session, add_phone_number
):
    call, wire = await _seed_member(async_session, add_phone_number)
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
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": wire,
            "outcome": "answered",
            "sip_status": None,
            "ring_ms": 3000,
        },
    )
    assert r.json() == {"ok": True}
    ev = (
        await async_session.execute(
            select(CallEvent).where(
                CallEvent.call_id == call.id, CallEvent.kind == "handover"
            )
        )
    ).scalar_one()
    assert ev.payload["contact_id"] == wire
    assert ev.payload["name"] == "Ana"
    delivery = (
        await async_session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.event_type == "call.transferred"
            )
        )
    ).scalar_one()
    assert delivery.payload["data"]["transfer"] == {
        "contact_id": wire,
        "contact_name": "Ana",
    }


async def test_result_for_member_who_left_org_still_recorded(
    client, async_session, add_phone_number
):
    call, wire = await _seed_member(async_session, add_phone_number, in_org=False)
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": wire,
            "outcome": "no_answer",
            "sip_status": 480,
            "ring_ms": 30000,
        },
    )
    assert r.json() == {"ok": True}


async def test_handover_denied_when_agent_org_differs_from_call(
    client, async_session, add_phone_number
):
    """Defense in depth: a call pointing at another org's agent dials no one."""
    call, contact = await _seed(async_session, add_phone_number)
    agent = await async_session.get(Agent, call.agent_id)
    other = uuid.uuid4()
    agent.organization_id = other
    contact.organization_id = other
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )
    assert r.json()["ok"] is False


# --- text to the contact while the agent dials ----------------------------


def _sms_payload(call, contact, invocation=None, reason="a leak"):
    return {
        "tool_invocation_id": str(invocation or uuid.uuid4()),
        "call_id": str(call.id),
        "contact_id": str(contact.id),
        "reason": reason,
    }


async def test_handover_sms_sent_from_the_call_number(
    client, async_session, add_phone_number, sms_mock
):
    call, contact = await _seed(async_session, add_phone_number)
    await approve_us_sms(async_session, call.organization_id)
    await async_session.commit()
    payload = _sms_payload(call, contact)
    r = await _post(client, "/internal/agent/handover-sms", payload)
    assert r.json() == {"sms": "sent"}
    # A retried post learns the outcome and sends nothing more.
    r = await _post(client, "/internal/agent/handover-sms", payload)
    assert r.json() == {"sms": "sent"}
    [row] = (await async_session.execute(select(Sms))).scalars().all()
    assert row.to_e164 == "+14155550120" and row.from_e164 == call.from_e164
    assert row.body == (
        "Desk (AI agent) is calling you now to connect a caller "
        "(+14155550199). Reason given: a leak."
    )
    assert row.metadata_["tool_invocation_id"] == payload["tool_invocation_id"]
    assert sms_mock.send_sms.await_count == 1
    # A second dial to the same person on this call texts nothing more.
    r = await _post(client, "/internal/agent/handover-sms", _sms_payload(call, contact))
    assert r.json() == {"sms": "skipped:already_texted"}
    assert sms_mock.send_sms.await_count == 1


async def test_handover_sms_blocked_without_us_registration(
    client, async_session, add_phone_number, sms_mock
):
    call, contact = await _seed(async_session, add_phone_number)
    r = await _post(client, "/internal/agent/handover-sms", _sms_payload(call, contact))
    assert r.json() == {"sms": "blocked:us_registration"}
    assert (await async_session.execute(select(Sms))).scalars().all() == []
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "agent.handover.sms_blocked")
        )
    ).scalar_one()
    assert audit.payload["reason"] == "us_registration"
    sms_mock.send_sms.assert_not_awaited()


@pytest.mark.parametrize("case", ["voice_only", "ended"])
async def test_handover_sms_skipped(
    client, async_session, add_phone_number, sms_mock, case
):
    call, contact = await _seed(async_session, add_phone_number)
    await approve_us_sms(async_session, call.organization_id)
    if case == "voice_only":
        number = await async_session.get(PhoneNumber, call.from_number_id)
        number.capabilities = ["voice"]
    else:
        call.status = "completed"
        call.end_reason = "normal_hangup"
    await async_session.commit()
    r = await _post(client, "/internal/agent/handover-sms", _sms_payload(call, contact))
    expected = "number_cannot_text" if case == "voice_only" else "call_ended"
    assert r.json() == {"sms": f"skipped:{expected}"}
    sms_mock.send_sms.assert_not_awaited()


# --- email to the contact while the agent dials ---------------------------


async def _with_sender(s, call, contact, email="sam@a.test"):
    contact.email = email
    s.add(
        EmailDomain(
            organization_id=call.organization_id,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    await s.commit()


async def test_handover_email_sent_with_the_text_words(
    client, async_session, add_phone_number, email_mock
):
    call, contact = await _seed(async_session, add_phone_number)
    await _with_sender(async_session, call, contact)
    payload = _sms_payload(call, contact)
    r = await _post(client, "/internal/agent/handover-email", payload)
    assert r.json() == {"email": "sent"}
    # A retried post learns the outcome and sends nothing more.
    r = await _post(client, "/internal/agent/handover-email", payload)
    assert r.json() == {"email": "sent"}
    [row] = (await async_session.execute(select(Email))).scalars().all()
    assert row.to_addresses == ["sam@a.test"]
    assert row.subject == "Desk is calling you to connect a caller"
    assert row.body_text == (
        "Desk (AI agent) is calling you now to connect a caller "
        "(+14155550199). Reason given: a leak."
    )
    assert row.metadata_["call_id"] == str(call.id)
    # A second dial to the same person on this call emails nothing more.
    r = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    assert r.json() == {"email": "skipped:already_emailed"}
    assert len((await async_session.execute(select(Email))).scalars().all()) == 1


async def test_handover_email_goes_out_without_us_text_registration(
    client, async_session, add_phone_number, email_mock
):
    """A number without US texting registration cannot text the contact; the
    email still tells them who is calling."""
    call, contact = await _seed(async_session, add_phone_number)
    await _with_sender(async_session, call, contact)
    sms = await _post(
        client, "/internal/agent/handover-sms", _sms_payload(call, contact)
    )
    mail = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    assert sms.json() == {"sms": "blocked:us_registration"}
    assert mail.json() == {"email": "sent"}


@pytest.mark.parametrize("case", ["no_email", "no_sender", "ended"])
async def test_handover_email_skipped(
    client, async_session, add_phone_number, email_mock, case
):
    call, contact = await _seed(async_session, add_phone_number)
    if case != "no_email":
        await _with_sender(async_session, call, contact)
    if case == "no_sender":
        for d in (await async_session.execute(select(EmailDomain))).scalars():
            await async_session.delete(d)
    if case == "ended":
        call.status = "completed"
        call.end_reason = "normal_hangup"
    await async_session.commit()
    r = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    expected = "call_ended" if case == "ended" else case
    assert r.json() == {"email": f"skipped:{expected}"}
    assert (await async_session.execute(select(Email))).scalars().all() == []


async def test_handover_email_blocked_by_suppression(
    client, async_session, add_phone_number, email_mock
):
    call, contact = await _seed(async_session, add_phone_number)
    await _with_sender(async_session, call, contact)
    await add_suppression(
        async_session,
        organization_id=call.organization_id,
        recipient="sam@a.test",
        channel="email",
        reason="unsubscribe",
        source="test",
    )
    await async_session.commit()
    r = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    assert r.json() == {"email": "blocked:compliance"}
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "agent.handover.email_blocked")
        )
    ).scalar_one()
    assert audit.payload["reason"] == "compliance"


async def test_handover_email_to_team_member_uses_login_email(
    client, async_session, add_phone_number, email_mock
):
    call, wire = await _seed_member(async_session, add_phone_number)
    async_session.add(
        EmailDomain(
            organization_id=call.organization_id,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    await async_session.commit()
    r = await _post(
        client,
        "/internal/agent/handover-email",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": wire,
            "reason": "",
        },
    )
    assert r.json() == {"email": "sent"}
    [row] = (await async_session.execute(select(Email))).scalars().all()
    assert row.to_addresses and "@" in row.to_addresses[0]


async def test_result_records_the_email_outcome(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "no_answer",
            "ring_ms": 30000,
            "sms": "blocked:us_registration",
            "email": "sent",
        },
    )
    assert r.json() == {"ok": True}
    event = (
        await async_session.execute(
            select(CallEvent).where(CallEvent.kind == "handover")
        )
    ).scalar_one()
    assert event.payload["email"] == "sent"


async def test_notices_still_go_out_after_a_fast_pickup(
    client, async_session, add_phone_number, email_mock, sms_mock
):
    """A voicemail picks up at once and the handover is recorded answered
    before the text and email run: both still go out, since that is when
    the contact needs the caller's number most."""
    call, contact = await _seed(async_session, add_phone_number)
    await approve_us_sms(async_session, call.organization_id)
    await _with_sender(async_session, call, contact)
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "answered",
            "ring_ms": 1000,
        },
    )
    assert r.json() == {"ok": True}
    sms = await _post(
        client, "/internal/agent/handover-sms", _sms_payload(call, contact)
    )
    mail = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    assert sms.json() == {"sms": "sent"} and mail.json() == {"email": "sent"}


async def test_result_records_a_cancelled_dial_and_ignores_unknown_fields(
    client, async_session, add_phone_number
):
    call, contact = await _seed(async_session, add_phone_number)
    r = await _post(
        client,
        "/internal/agent/handover-result",
        {
            "tool_invocation_id": str(uuid.uuid4()),
            "call_id": str(call.id),
            "contact_id": str(contact.id),
            "outcome": "cancelled",
            "ring_ms": 4000,
            "from_a_newer_voicebot": True,
        },
    )
    assert r.json() == {"ok": True}
    event = (
        await async_session.execute(
            select(CallEvent).where(CallEvent.kind == "handover")
        )
    ).scalar_one()
    assert event.payload["outcome"] == "cancelled"
    assert "from_a_newer_voicebot" not in event.payload


async def test_handover_email_without_sender_writes_no_block(
    client, async_session, add_phone_number, email_mock
):
    """No verified sending domain: skipped before any send check runs."""
    call, contact = await _seed(async_session, add_phone_number)
    contact.email = "sam@a.test"
    await async_session.commit()
    r = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    assert r.json() == {"email": "skipped:no_sender"}
    blocked = (
        (
            await async_session.execute(
                select(AuditLog).where(
                    AuditLog.action == "agent.handover.email_blocked"
                )
            )
        )
        .scalars()
        .all()
    )
    assert blocked == []


async def test_handover_email_subject_is_one_short_line(
    client, async_session, add_phone_number, email_mock
):
    call, contact = await _seed(async_session, add_phone_number)
    await _with_sender(async_session, call, contact)
    agent = await async_session.get(Agent, call.agent_id)
    agent.name = "Front\r\nBcc: x@evil.test " + "y" * 200
    await async_session.commit()
    r = await _post(
        client, "/internal/agent/handover-email", _sms_payload(call, contact)
    )
    assert r.json() == {"email": "sent"}
    [row] = (await async_session.execute(select(Email))).scalars().all()
    assert "\n" not in row.subject and "\r" not in row.subject
    assert row.subject.startswith("Front Bcc: x@evil.test y")
    assert len(row.subject) <= 80 + len(" is calling you to connect a caller")


async def test_result_follow_up_records_the_finished_notices(
    client, async_session, add_phone_number
):
    """The answer is posted at once with the text and email still pending;
    the voicebot's second post with the same id fills them in, and changes
    nothing else."""
    call, contact = await _seed(async_session, add_phone_number)
    body = {
        "tool_invocation_id": str(uuid.uuid4()),
        "call_id": str(call.id),
        "contact_id": str(contact.id),
        "outcome": "answered",
        "ring_ms": 1000,
        "sms": "pending",
        "email": "pending",
    }
    assert (await _post(client, "/internal/agent/handover-result", body)).json() == {
        "ok": True
    }
    update = {**body, "ring_ms": 99, "sms": "sent", "email": "pending"}
    assert (await _post(client, "/internal/agent/handover-result", update)).json() == {
        "ok": True
    }
    [event] = (
        await async_session.execute(
            select(CallEvent).where(CallEvent.kind == "handover")
        )
    ).scalars()
    await async_session.refresh(event)
    assert event.payload["sms"] == "sent" and event.payload["email"] == "pending"
    assert event.payload["ring_ms"] == 1000
    deliveries = (
        (
            await async_session.execute(
                select(WebhookDelivery).where(
                    WebhookDelivery.event_type == "call.transferred"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(deliveries) <= 1


# --- connected calls: handover through the phone system --------------------

AGENT_MODULE = "hailhq.api.routes.internal.agent"


async def _seed_pbx(
    s,
    *,
    caller="+14155550199",
    extension="800",
    phone=None,
    trunk="ST_out_pbx",
    provider="pbx",
    billed=False,
):
    """An inbound call to ``+14155550100`` from ``caller``, on a connected
    number (``provider="pbx"``) or a carrier number, handed to one contact."""
    org = uuid.uuid4()
    conn = SipConnection(
        organization_id=org,
        label="Main office",
        inbound_username="hail-" + uuid.uuid4().hex[:12],
        encrypted_inbound_password="token",
        default_region="US",
        pbx_host="pbx.example.com" if trunk else None,
        livekit_outbound_trunk_id=trunk,
    )
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    contact = Contact(
        organization_id=org, name="Support line", phone_e164=phone, extension=extension
    )
    s.add_all([conn, agent, contact])
    if billed:
        s.add(
            AccountCredit(
                organization_id=org,
                kind="credit",
                channel="credit",
                amount_cents=10_000,
                source="test",
                ref="test",
            )
        )
    await s.flush()
    s.add(
        AgentHandoverContact(
            agent_id=agent.id, contact_id=contact.id, note="Front desk", position=0
        )
    )
    pbx = provider == "pbx"
    number = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        capabilities=["voice"] if pbx else ["voice", "sms"],
        provider=provider,
        provider_resource_id=None if pbx else "PN_test",
        provisioning_state="active",
        sip_connection_id=conn.id if pbx else None,
    )
    s.add(number)
    await s.flush()
    call = Call(
        organization_id=org,
        direction="inbound",
        to_number_id=number.id,
        from_e164=caller,
        to_e164=number.e164,
        status="in_progress",
        agent_id=agent.id,
        provider=provider,
        voice_config={},
        metadata_={CALL_META_BILLED: True} if billed else {},
    )
    s.add(call)
    await s.commit()
    return call, contact


async def _post_handover(client, call, contact):
    return await _post(
        client,
        "/internal/agent/handover",
        {"call_id": str(call.id), "contact_id": str(contact.id)},
    )


async def _block_reason(s, call) -> str:
    audit = (
        await s.execute(
            select(AuditLog).where(
                AuditLog.resource_id == call.id,
                AuditLog.action == "agent.handover.blocked",
            )
        )
    ).scalar_one()
    return audit.payload["reason"]


@pytest.fixture()
def gate_mock(monkeypatch):
    mock = AsyncMock(return_value=GateResult(allowed=True))
    monkeypatch.setattr(f"{AGENT_MODULE}.check_handover_allowed", mock)
    return mock


async def test_handover_to_extension_on_pbx_call(client, async_session):
    call, contact = await _seed_pbx(async_session)
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is True
    assert body["to_e164"] == "800"
    assert body["from_e164"] == "+14155550199"  # the original caller
    assert body["trunk_id"] == "ST_out_pbx"
    assert body["headers"] is None


@pytest.mark.parametrize("allowed", [False, True])
async def test_handover_to_phone_on_pbx_call_keeps_gate(
    client, async_session, gate_mock, allowed
):
    call, contact = await _seed_pbx(async_session, extension=None, phone="+14155550123")
    gate_mock.return_value = GateResult(allowed=allowed, reason="suppressed")
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is allowed
    gate_mock.assert_awaited_once()
    assert gate_mock.await_args.args[2] == "+14155550123"
    if allowed:
        assert body["to_e164"] == "+14155550123"
        assert body["from_e164"] == "+14155550199"
        assert body["trunk_id"] == "ST_out_pbx"
    else:
        assert await _block_reason(async_session, call) == "suppressed"


async def test_handover_to_contact_with_phone_and_extension_dials_extension(
    client, async_session, gate_mock
):
    call, contact = await _seed_pbx(async_session, phone="+14155550123")
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is True
    assert body["to_e164"] == "800"
    gate_mock.assert_not_awaited()


async def test_handover_refuses_extension_on_the_line(client, async_session):
    call, contact = await _seed_pbx(async_session, caller="101", extension="101")
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is False
    assert body["spoken"] == ON_CALL_LINE.format(name="Support line")
    assert await _block_reason(async_session, call) == "contact_is_on_call"


async def test_handover_from_extension_caller_keeps_extension(client, async_session):
    call, contact = await _seed_pbx(async_session, caller="101")
    assert (await _post_handover(client, call, contact)).json()["from_e164"] == "101"


@pytest.mark.parametrize(
    "caller", ["anonymous", "", "sip:101@pbx.example.com", "1234567", "+0123"]
)
async def test_handover_from_anonymous_caller_uses_hail_number(
    client, async_session, caller
):
    call, contact = await _seed_pbx(async_session, caller=caller)
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is True
    assert body["from_e164"] == "+14155550100"


async def test_handover_without_pbx_host_is_denied(client, async_session):
    call, contact = await _seed_pbx(async_session, trunk=None)
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is False
    assert body.get("trunk_id") is None
    assert await _block_reason(async_session, call) == "carrier_route_failed"


async def test_extension_contact_denied_on_carrier_call(client, async_session):
    call, contact = await _seed_pbx(async_session, provider="twilio")
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is False
    assert body.get("trunk_id") is None


async def test_carrier_call_dials_the_phone_of_a_contact_with_an_extension(
    client, async_session
):
    call, contact = await _seed_pbx(
        async_session, provider="twilio", phone="+14155550123"
    )
    body = (await _post_handover(client, call, contact)).json()
    assert body["ok"] is True
    assert body["to_e164"] == "+14155550123"
    assert body["from_e164"] == "+14155550100"  # the Hail number, as before
    assert body["trunk_id"] == "ST_out_tw"


async def test_handover_quote_on_pbx_call_sends_two_pbx_legs(
    client, async_session, monkeypatch
):
    quote_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(f"{AGENT_MODULE}.quote_call_price", quote_mock)
    monkeypatch.setattr(f"{AGENT_MODULE}.quotes_enabled", lambda: True)
    call, contact = await _seed_pbx(async_session, billed=True)
    assert (await _post_handover(client, call, contact)).json()["ok"] is True
    legs = max((c.args[1] for c in quote_mock.await_args_list), key=len)
    assert [(leg["direction"], leg["carrier"], leg["number_e164"]) for leg in legs] == [
        ("inbound", "pbx", "+14155550100"),
        ("outbound", "pbx", "800"),
    ]


# Passes before and after the connected-number handover (the route already
# skips a contact with no phone). Kept as a regression test.
async def test_handover_notice_to_extension_contact_skips_text(
    client, async_session, sms_mock
):
    call, contact = await _seed_pbx(async_session)
    r = await _post(client, "/internal/agent/handover-sms", _sms_payload(call, contact))
    assert r.json() == {"sms": "skipped:no_contact"}
    sms_mock.send_sms.assert_not_awaited()


@pytest.mark.parametrize("value", ["101", "+14155550100", "123456"])
def test_pbx_caller_id_accepts_ascii_digits(value):
    from hailhq.api.routes.internal.agent import _PBX_CALLER_ID

    assert _PBX_CALLER_ID.fullmatch(value)


@pytest.mark.parametrize("value", ["١٠١", "+١٤١٥٥٥٥٠١٠٠", "1234567"])
def test_pbx_caller_id_refuses_other_digits(value):
    from hailhq.api.routes.internal.agent import _PBX_CALLER_ID

    assert not _PBX_CALLER_ID.fullmatch(value)
