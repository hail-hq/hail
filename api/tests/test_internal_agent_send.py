"""Internal agent-send routes: auth, call gating, cap, dedupe, org scoping."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.api.main import app
from hailhq.core import hmac_signing
from hailhq.core.agent_caps import AGENT_OUTBOUND_DISABLED_FLAG
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.config import settings
from hailhq.core.db import get_session
from hailhq.core.models import (
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
    call = await _insert_live_call(
        async_session, org, add_phone_number, to_e164="+14155550123"
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
