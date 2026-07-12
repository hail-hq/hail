"""Internal agent-send routes: auth, call gating, cap, dedupe, org scoping."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core import hmac_signing
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.config import settings
from hailhq.core.models import (
    Call,
    Email,
    EmailDomain,
    OrganizationMember,
    PhoneNumber,
    Sms,
    User,
)

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
    session: AsyncSession, org_id, *, to_e164="+14155550123", billed=False
) -> Call:
    # Follow test_calls_api.py / test_internal_dsar.py's Call-row insertion
    # pattern for required columns (from_number_id needs a PhoneNumber row;
    # PhoneNumber itself requires country_code/number_type/
    # provider_resource_id — all NOT NULL with no server default).
    number = PhoneNumber(
        organization_id=org_id,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider_resource_id=f"PN-{uuid.uuid4()}",
        capabilities=["voice", "sms"],
        provisioning_state="active",
        is_pool=False,
    )
    session.add(number)
    await session.flush()
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


async def test_send_sms_ended_call_is_denied(client, async_session):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org)
    call.status = "completed"
    call.end_reason = "normal_hangup"
    await async_session.commit()
    body = _sms_payload(call.id)
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["ok"] is False


async def test_send_sms_happy_path_targets_counterpart(client, async_session, sms_mock):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org, to_e164="+14155550123")
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
    client, async_session, sms_mock
):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org)
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


async def test_send_sms_cap_blocks_sixth_send(client, async_session, sms_mock):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org)
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
    client, async_session, email_mock
):
    org_a, org_b = uuid.uuid4(), uuid.uuid4()
    call = await _insert_live_call(async_session, org_a)
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


async def test_send_email_happy_path_stamps_call_id(client, async_session, email_mock):
    org = uuid.uuid4()
    call = await _insert_live_call(async_session, org)
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


async def test_send_sms_rejects_when_secret_unconfigured(
    client, async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "")
    body = _sms_payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/send-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 503
