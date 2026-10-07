"""Internal agent-send routes: auth, call gating, cap, dedupe, org scoping."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.api.main import app
from hailhq.api.numbers import resolve_sms_number
from hailhq.api.routes.internal.agent import _SPOKEN_SMS_UNCONFIGURED
from hailhq.core import hmac_signing
from hailhq.core.agent_caps import AGENT_OUTBOUND_DISABLED_FLAG
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import (
    Agent,
    AuditLog,
    Call,
    Email,
    EmailDomain,
    Organization,
    OrganizationMember,
    PlatformFlag,
    Sms,
    User,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

HMAC_SECRET = "test-internal-secret"


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, HMAC_SECRET),
    }


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


async def _insert_live_call(
    session: AsyncSession,
    org_id,
    add_phone_number,
    *,
    to_e164="+14155550123",
    billed=False,
    agent_id=None,
) -> Call:
    # add_phone_number (conftest.py factory fixture, see test_calls_api.py)
    # covers the same required columns this used to hand-roll: PhoneNumber
    # needs country_code/number_type/provider_resource_id, all NOT NULL with
    # no server default.
    number = await add_phone_number(session, org_id)
    call = Call(
        organization_id=org_id,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164=to_e164,
        status="in_progress",
        agent_id=agent_id,
        voice_config={},
        metadata_={CALL_META_BILLED: billed},
    )
    session.add(call)
    await session.commit()
    return call


def _sms_payload(call_id, body="hello"):
    return json.dumps(
        {
            "call_id": str(call_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": body,
        }
    ).encode()


async def test_send_sms_rejects_bad_signature(
    client: httpx.AsyncClient, async_session: AsyncSession
):
    body = _sms_payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/send-sms",
        content=body,
        headers={"X-Hail-Signature": "sha256=deadbeef"},
    )
    assert resp.status_code == 401


async def test_send_sms_unknown_call_is_spoken_denial(client, async_session):
    body = _sms_payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert data["spoken"]


async def test_send_sms_ended_call_is_denied(client, async_session, add_phone_number):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    call.status = "completed"
    call.end_reason = "normal_hangup"
    await async_session.commit()
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is False


async def test_send_sms_happy_path_targets_counterpart(
    client, async_session, sms_mock, add_phone_number
):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Help.")
    async_session.add(agent)
    await async_session.flush()
    call = await _insert_live_call(
        async_session,
        org,
        add_phone_number,
        to_e164="+14155550123",
        agent_id=agent.id,
    )
    body = _sms_payload(call.id, body="Your code is 42.")
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    data = resp.json()
    assert data["ok"] is True

    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert len(rows) == 1
    assert rows[0].to_e164 == "+14155550123"  # always the counterpart
    meta = rows[0].metadata
    assert meta["call_id"] == str(call.id)
    assert rows[0].agent_id == agent.id


async def test_send_sms_replays_same_invocation_without_double_send(
    client, async_session, sms_mock, add_phone_number
):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    payload = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": "hi",
    }
    body = json.dumps(payload).encode()
    r1 = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    r2 = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert r1.json()["ok"] is True and r2.json()["ok"] is True
    count = len((await async_session.execute(Sms.__table__.select())).fetchall())
    assert count == 1


async def test_send_sms_cap_blocks_sixth_send(
    client, async_session, sms_mock, add_phone_number
):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    for _ in range(5):
        body = _sms_payload(call.id)
        assert (
            await client.post(
                "/internal/agent/send-sms", content=body, headers=_signed(body)
            )
        ).json()["ok"] is True
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is False


async def test_send_email_resolves_member_and_never_crosses_orgs(
    client, async_session, email_mock, add_phone_number
):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    call = await _insert_live_call(async_session, org_a, add_phone_number)
    async_session.add(
        EmailDomain(
            organization_id=org_a,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    user = User(
        id=uuid.uuid4(),
        name="Sarah Chen",
        email="sarah@b.test",
        created_at=datetime.now(timezone.utc),
    )
    async_session.add(user)
    # Sarah is a member of org B only — org A's call must NOT reach her.
    async_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org_b,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    payload = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Sarah Chen",
            "subject": "Call summary",
            "body_text": "Hello.",
        }
    ).encode()
    resp = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )
    data = resp.json()
    assert data["ok"] is False  # not found in org A's directory
    assert "sarah@b.test" not in data["spoken"]  # never leak the address
    rows = (await async_session.execute(Email.__table__.select())).fetchall()
    assert rows == []


async def test_send_email_happy_path_stamps_call_id(
    client, async_session, email_mock, add_phone_number
):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    async_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    user = User(
        id=uuid.uuid4(),
        name="Sarah Chen",
        email="sarah@a.test",
        created_at=datetime.now(timezone.utc),
    )
    async_session.add(user)
    async_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    payload = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Sarah Chen",
            "subject": "Call summary",
            "body_text": "Hello from the call.",
        }
    ).encode()
    resp = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )
    assert resp.json()["ok"] is True
    rows = (await async_session.execute(Email.__table__.select())).fetchall()
    assert len(rows) == 1
    assert rows[0].to_addresses == ["sarah@a.test"]
    assert rows[0].metadata["call_id"] == str(call.id)


async def test_send_sms_concurrent_same_invocation_sends_once(
    client, async_session, session_factory, sms_mock, add_phone_number
):
    """Two racing requests with one tool_invocation_id: exactly one row.

    The client fixture's get_session override yields one shared session,
    which would serialize the requests artificially (and break on
    concurrent use). Swap in a per-request session for the gather so the
    two handlers hold independent transactions and the call-row FOR UPDATE
    lock is what serializes them — this runs against real Postgres.
    """
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    payload = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": "hi",
    }
    body = json.dumps(payload).encode()

    async def per_request_session():
        async with session_factory() as s:
            yield s

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    try:
        r1, r2 = await asyncio.gather(
            client.post(
                "/internal/agent/send-sms", content=body, headers=_signed(body)
            ),
            client.post(
                "/internal/agent/send-sms", content=body, headers=_signed(body)
            ),
        )
    finally:
        app.dependency_overrides[get_session] = saved

    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["ok"] is True and r2.json()["ok"] is True
    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert len(rows) == 1


async def test_send_sms_suppression_blocks_agent_send(
    client, async_session, add_phone_number
):
    org = uuid.uuid4()
    call = await _insert_live_call(
        async_session, org, add_phone_number, to_e164="+14155550123"
    )
    await add_suppression(
        async_session,
        organization_id=org,
        recipient="+14155550123",
        channel="sms",
        reason="recipient_request",
        source="manual",
    )
    await async_session.commit()

    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "+14155550123" not in data["spoken"]
    assert "suppress" not in data["spoken"].lower()

    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert rows == []


async def test_send_sms_denied_when_org_has_no_funds(
    client, async_session, add_phone_number
):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number, billed=True)
    # No account_credits rows for this org — balance defaults to zero.
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False

    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert rows == []


async def test_send_sms_rejects_when_secret_unconfigured(
    client, async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "")
    body = _sms_payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 503


async def test_send_sms_retry_after_call_ended_still_dedupes(
    client, async_session, sms_mock, add_phone_number
):
    """A retry with the SAME tool_invocation_id must hit the dedupe branch
    even after the call has finalized — the original send may have still
    been mid-flight when the call ended. Regression test for reordering
    the dedupe lookup ahead of the liveness check."""
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    payload = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": "hi",
    }
    body = json.dumps(payload).encode()
    r1 = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert r1.json()["ok"] is True

    call.status = "completed"
    call.end_reason = "normal_hangup"
    await async_session.commit()

    r2 = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is True

    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert len(rows) == 1


async def test_send_email_replay_bounced_is_not_ok(
    client, async_session, email_mock, add_phone_number
):
    """Replay must reflect a bounce that landed between attempts (the SES
    webhook can flip sent→bounced), mirroring the SMS path's failed/
    undelivered exclusion."""
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    async_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="mail.a.test",
            verification_status="verified",
        )
    )
    user = User(
        id=uuid.uuid4(),
        name="Sarah Chen",
        email="sarah@a.test",
        created_at=datetime.now(timezone.utc),
    )
    async_session.add(user)
    async_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    payload = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Sarah Chen",
            "subject": "Call summary",
            "body_text": "Hello from the call.",
        }
    ).encode()
    r1 = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )
    assert r1.json()["ok"] is True

    await async_session.execute(
        Email.__table__.update().values(status="bounced", end_reason="bounced")
    )
    await async_session.commit()

    r2 = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )
    assert r2.status_code == 200
    assert r2.json()["ok"] is False

    rows = (await async_session.execute(Email.__table__.select())).fetchall()
    assert len(rows) == 1  # still no second row — this was a replay


async def test_send_sms_provider_failure_writes_send_failed_audit(
    client, async_session, sms_mock, add_phone_number
):
    sms_mock.send_sms.side_effect = Exception("carrier down")
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is False

    rows = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "agent.sms.send_failed")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].organization_id == org
    assert rows[0].payload["end_reason"] == "provider_error"


async def test_send_sms_blocked_by_agent_kill_switch(
    client, async_session, sms_mock, add_phone_number
):
    """Voicebot sends must honor the platform agent caps: an agent-origin
    org with the kill switch on gets a vague spoken denial and no row."""
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    async_session.add(Organization(id=org, origin="agent"))
    async_session.add(PlatformFlag(key=AGENT_OUTBOUND_DISABLED_FLAG, value="true"))
    await async_session.commit()

    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is False
    assert "kill" not in data["spoken"].lower()
    assert "disabled" not in data["spoken"].lower()
    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert rows == []


async def test_send_sms_agent_caps_noop_for_human_org(
    client, async_session, sms_mock, add_phone_number
):
    """Kill switch on, but the org is human-origin: send goes through."""
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    async_session.add(Organization(id=org, origin="human"))
    async_session.add(PlatformFlag(key=AGENT_OUTBOUND_DISABLED_FLAG, value="true"))
    await async_session.commit()

    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is True


async def test_agent_send_email_still_picks_a_sender_with_several_verified(
    client, async_session, email_mock, add_phone_number
):
    """The voicebot keeps the oldest-verified pick POST /emails now refuses.

    A voice agent has no way to name a sending domain mid-call, so the
    ambiguity that returns 422 on the public route must not silently turn
    into "email is not configured" here.
    """
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, add_phone_number)
    now = datetime.now(timezone.utc)
    async_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="first.test",
            verification_status="verified",
            created_at=now - timedelta(days=1),
        )
    )
    async_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="second.test",
            verification_status="verified",
            created_at=now,
        )
    )
    user = User(
        id=uuid.uuid4(),
        name="Sarah Chen",
        email="sarah@a.test",
        created_at=now,
    )
    async_session.add(user)
    async_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org,
            role="member",
            created_at=now,
        )
    )
    await async_session.commit()

    payload = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Sarah Chen",
            "subject": "Call summary",
            "body_text": "Hello from the call.",
        }
    ).encode()
    resp = await client.post(
        "/internal/agent/send-email", content=payload, headers=_signed(payload)
    )

    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    rows = (await async_session.execute(select(Email))).scalars().all()
    assert len(rows) == 1
    assert rows[0].from_address == "noreply@first.test"


async def test_send_sms_on_inbound_call_texts_the_caller_from_the_dialed_number(
    client, async_session, sms_mock, add_phone_number
):
    """Inbound: the person is `from_e164`; the reply goes out from the
    number they dialed."""
    org = uuid.uuid4()
    number = await add_phone_number(async_session, org)
    call = Call(
        organization_id=org,
        to_number_id=number.id,
        from_e164="+33612345678",
        to_e164=number.e164,
        direction="inbound",
        status="in_progress",
        voice_config={},
        metadata_={CALL_META_BILLED: False},
    )
    async_session.add(call)
    await async_session.commit()
    body = _sms_payload(call.id, body="Here is the link.")
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is True
    rows = (await async_session.execute(Sms.__table__.select())).fetchall()
    assert len(rows) == 1
    assert rows[0].to_e164 == "+33612345678"
    assert rows[0].from_e164 == number.e164


async def _inbound_call_on_voice_only_number(
    session, add_phone_number, *, sms_agent_id="same"
):
    """Agent A, voice-only number V (dialed), and one SMS number S."""
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="A", system_prompt="x")
    other = Agent(organization_id=org, name="B", system_prompt="x")
    session.add_all([agent, other])
    await session.commit()
    voice = await add_phone_number(
        session, org, e164="+14155550001", provider_resource_id="PN_V"
    )
    voice.capabilities = ["voice"]
    voice.voice_agent_id = agent.id
    sms_number = await add_phone_number(
        session, org, e164="+14155550002", provider_resource_id="PN_S"
    )
    sms_number.capabilities = ["sms"]
    sms_number.sms_agent_id = {"same": agent.id, "other": other.id, "none": None}[
        sms_agent_id
    ]
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=voice.id,
        from_e164="+14155550123",
        to_e164=voice.e164,
        direction="inbound",
        status="in_progress",
        voice_config={},
        metadata_={CALL_META_BILLED: False},
    )
    session.add(call)
    await session.commit()
    return agent, other, voice, sms_number, call


async def test_voice_only_dialed_number_texts_from_the_agents_sms_number(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="same"
    )
    body = _sms_payload(call.id, body="Your code is 42.")
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == sms_number.id
    assert sent.agent_id == agent.id


async def test_voice_only_dialed_number_binds_an_unbound_sms_number(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="none"
    )
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is True
    await async_session.refresh(sms_number)
    assert sms_number.sms_agent_id == agent.id


async def test_sms_number_bound_to_another_agent_is_not_taken(
    client, async_session, sms_mock, add_phone_number
):
    _, other, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="other"
    )
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    data = resp.json()
    assert data["ok"] is False
    assert data["spoken"] == _SPOKEN_SMS_UNCONFIGURED
    await async_session.refresh(sms_number)
    assert sms_number.sms_agent_id == other.id
    assert (await async_session.execute(select(Sms))).scalars().all() == []


async def _send(client, call):
    body = _sms_payload(call.id)
    return await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )


async def test_bind_writes_a_system_audit_entry(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="none"
    )
    assert (await _send(client, call)).json()["ok"] is True
    rows = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "number.route")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].actor_kind == "system"
    assert rows[0].resource_id == sms_number.id
    assert rows[0].payload["sms_agent_id"] == str(agent.id)
    assert rows[0].payload["automatic"] is True


async def test_already_routed_number_writes_no_route_audit(
    client, async_session, sms_mock, add_phone_number
):
    _, _, _, _, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="same"
    )
    assert (await _send(client, call)).json()["ok"] is True
    rows = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "number.route")
            )
        )
        .scalars()
        .all()
    )
    assert rows == []


async def _route_audits(session):
    return (
        (
            await session.execute(
                select(AuditLog).where(AuditLog.action == "number.route")
            )
        )
        .scalars()
        .all()
    )


async def test_agent_without_sms_enabled_is_not_bound(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, _, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="none"
    )
    agent.sms_enabled = False
    await async_session.commit()
    assert (await _send(client, call)).json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == sms_number.id
    await async_session.refresh(sms_number)
    assert sms_number.sms_agent_id is None
    assert await _route_audits(async_session) == []


async def _inbound_call_on_sms_number(session, add_phone_number, *, dialed_agent):
    """Agent A dials-in on SMS number D (bound per ``dialed_agent``); org also
    has an older-created unbound SMS number S2 and a number routed to A."""
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="A", system_prompt="x")
    other = Agent(organization_id=org, name="B", system_prompt="x")
    session.add_all([agent, other])
    await session.commit()
    dialed = await add_phone_number(
        session, org, e164="+14155550041", provider_resource_id="PN_D"
    )
    dialed.sms_agent_id = {"same": agent.id, "other": other.id, "none": None}[
        dialed_agent
    ]
    org_number = await add_phone_number(
        session, org, e164="+14155550042", provider_resource_id="PN_O"
    )
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=dialed.id,
        from_e164="+14155550123",
        to_e164=dialed.e164,
        direction="inbound",
        status="in_progress",
        voice_config={},
        metadata_={CALL_META_BILLED: False},
    )
    session.add(call)
    await session.commit()
    return agent, other, dialed, org_number, call


async def test_dialed_number_bound_to_this_agent_is_used(
    client, async_session, sms_mock, add_phone_number
):
    _, _, dialed, org_number, call = await _inbound_call_on_sms_number(
        async_session, add_phone_number, dialed_agent="same"
    )
    assert (await _send(client, call)).json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == dialed.id
    assert await _route_audits(async_session) == []
    await async_session.refresh(org_number)
    assert org_number.sms_agent_id is None


async def test_free_dialed_number_is_bound_and_audited(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, dialed, org_number, call = await _inbound_call_on_sms_number(
        async_session, add_phone_number, dialed_agent="none"
    )
    assert (await _send(client, call)).json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == dialed.id
    await async_session.refresh(dialed)
    await async_session.refresh(org_number)
    assert dialed.sms_agent_id == agent.id
    assert org_number.sms_agent_id is None
    audits = await _route_audits(async_session)
    assert len(audits) == 1
    assert audits[0].resource_id == dialed.id
    assert audits[0].payload["sms_agent_id"] == str(agent.id)


async def test_dialed_number_bound_to_another_agent_is_skipped(
    client, async_session, sms_mock, add_phone_number
):
    agent, other, dialed, org_number, call = await _inbound_call_on_sms_number(
        async_session, add_phone_number, dialed_agent="other"
    )
    assert (await _send(client, call)).json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == org_number.id
    await async_session.refresh(dialed)
    await async_session.refresh(org_number)
    assert dialed.sms_agent_id == other.id
    assert org_number.sms_agent_id == agent.id


async def test_free_dialed_number_without_sms_enabled_is_used_unbound(
    client, async_session, sms_mock, add_phone_number
):
    agent, _, dialed, _, call = await _inbound_call_on_sms_number(
        async_session, add_phone_number, dialed_agent="none"
    )
    agent.sms_enabled = False
    await async_session.commit()
    assert (await _send(client, call)).json()["ok"] is True
    sent = (await async_session.execute(select(Sms))).scalars().one()
    assert sent.from_number_id == dialed.id
    await async_session.refresh(dialed)
    assert dialed.sms_agent_id is None
    assert await _route_audits(async_session) == []


async def test_second_agent_does_not_take_a_number_the_first_bound(
    async_session, add_phone_number
):
    _, other, voice, sms_number, call = await _inbound_call_on_voice_only_number(
        async_session, add_phone_number, sms_agent_id="none"
    )
    org = call.organization_id
    first = await resolve_sms_number(async_session, org, call.agent_id, voice)
    assert first.id == sms_number.id
    await async_session.commit()
    assert await resolve_sms_number(async_session, org, other.id, voice) is None
    await async_session.refresh(sms_number)
    assert sms_number.sms_agent_id == call.agent_id


async def test_no_agent_keeps_oldest_sms_number(async_session, add_phone_number):
    org = uuid.uuid4()
    older = await add_phone_number(
        async_session, org, e164="+14155550011", provider_resource_id="PN_1"
    )
    other_agent = Agent(organization_id=org, name="B", system_prompt="x")
    async_session.add(other_agent)
    await async_session.commit()
    older.sms_agent_id = other_agent.id
    await add_phone_number(
        async_session, org, e164="+14155550012", provider_resource_id="PN_2"
    )
    await async_session.commit()
    got = await resolve_sms_number(async_session, org, None, None)
    assert got.id == older.id
    assert got.sms_agent_id == other_agent.id


async def test_routed_number_preferred_over_unbound_one(
    async_session, add_phone_number
):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="A", system_prompt="x")
    async_session.add(agent)
    await async_session.commit()
    unbound = await add_phone_number(
        async_session, org, e164="+14155550021", provider_resource_id="PN_1"
    )
    routed = await add_phone_number(
        async_session, org, e164="+14155550022", provider_resource_id="PN_2"
    )
    routed.sms_agent_id = agent.id
    await async_session.commit()
    got = await resolve_sms_number(async_session, org, agent.id, None)
    assert got.id == routed.id
    await async_session.refresh(unbound)
    assert unbound.sms_agent_id is None


async def test_inactive_dialed_number_falls_through(async_session, add_phone_number):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="A", system_prompt="x")
    async_session.add(agent)
    await async_session.commit()
    dialed = await add_phone_number(
        async_session,
        org,
        e164="+14155550031",
        provider_resource_id="PN_1",
        state="released",
    )
    routed = await add_phone_number(
        async_session, org, e164="+14155550032", provider_resource_id="PN_2"
    )
    routed.sms_agent_id = agent.id
    await async_session.commit()
    got = await resolve_sms_number(async_session, org, agent.id, dialed)
    assert got.id == routed.id
