"""Tests for POST /internal/dsar/{lookup,export,delete}."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.models import Call, PhoneNumber, Suppression

HMAC_SECRET = "test-internal-secret"
PHONE = "+14155551234"


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}", "Content-Type": "application/json"}


@pytest.fixture()
def internal_secret_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


async def _seed(session: AsyncSession, org_id: uuid.UUID) -> None:
    pn = PhoneNumber(
        organization_id=org_id,
        e164="+14155559000",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id=f"PN-{uuid.uuid4()}",
        provisioning_state="active",
    )
    session.add(pn)
    await session.flush()

    session.add(
        Call(
            organization_id=org_id,
            from_number_id=pn.id,
            from_e164=pn.e164,
            to_e164=PHONE,
            voice_config={"stt": "deepgram", "tts": "cartesia"},
            status="completed",
            end_reason="normal_hangup",
            transcript=[{"role": "user", "text": "hi"}],
        )
    )
    session.add(
        Suppression(
            organization_id=org_id,
            recipient=PHONE,
            channel="voice",
            reason="recipient_request",
            source="manual",
        )
    )
    await session.commit()


@pytest.mark.asyncio
async def test_rejects_missing_signature(
    client: httpx.AsyncClient, internal_secret_set
):
    resp = await client.post("/internal/dsar/lookup", json={"identifier": PHONE})
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_rejects_non_ascii_signature_with_401_not_500(
    client: httpx.AsyncClient, internal_secret_set
):
    # httpx.Headers normalizes str header values as strict ASCII, so a
    # non-ASCII value must be passed pre-encoded (as bytes) to reach the
    # ASGI app unchanged — this is a test-client quirk, not a change in
    # what's being exercised: the server still receives the literal
    # "sha256=héllo" header value verify() must reject without raising.
    resp = await client.post(
        "/internal/dsar/lookup",
        content=b'{"identifier": "+14155551234"}',
        headers={
            "X-Hail-Signature": "sha256=héllo".encode("utf-8"),
            "Content-Type": "application/json",
        },
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_lookup_finds_call_and_suppression(
    client: httpx.AsyncClient, async_session: AsyncSession, internal_secret_set
):
    org_id = uuid.uuid4()
    await _seed(async_session, org_id)

    body = json.dumps({"identifier": PHONE}).encode()
    resp = await client.post(
        "/internal/dsar/lookup", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["calls"]) == 1
    assert data["calls"][0]["to_e164"] == PHONE
    assert len(data["suppressions"]) == 1


@pytest.mark.asyncio
async def test_export_returns_serializable_structure(
    client: httpx.AsyncClient, async_session: AsyncSession, internal_secret_set
):
    org_id = uuid.uuid4()
    await _seed(async_session, org_id)

    body = json.dumps({"identifier": PHONE}).encode()
    resp = await client.post(
        "/internal/dsar/export", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["identifier"] == PHONE
    assert isinstance(data["calls"], list)


@pytest.mark.asyncio
async def test_delete_clears_content_preserves_suppression(
    client: httpx.AsyncClient, async_session: AsyncSession, internal_secret_set
):
    org_id = uuid.uuid4()
    await _seed(async_session, org_id)

    body = json.dumps({"identifier": PHONE}).encode()
    resp = await client.post(
        "/internal/dsar/delete", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["calls_scrubbed"] == 1
    assert data["suppressions_preserved"] == 1

    lookup_body = json.dumps({"identifier": PHONE}).encode()
    lookup_resp = await client.post(
        "/internal/dsar/lookup", content=lookup_body, headers=_signed(lookup_body)
    )
    lookup_data = lookup_resp.json()
    assert lookup_data["calls"][0]["transcript"] is None
    assert len(lookup_data["suppressions"]) == 1
