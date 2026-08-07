"""Tests for POST /internal/numbers/release (dunning release path)."""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid

import pytest
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from sqlalchemy.ext.asyncio import AsyncSession

HMAC_SECRET = "test-internal-secret"


def _signed(body: bytes) -> dict[str, str]:
    sig = hmac.new(HMAC_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return {"X-Hail-Signature": f"sha256={sig}", "Content-Type": "application/json"}


@pytest.fixture()
def internal_secret_set(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


async def _seed_number(
    session: AsyncSession, org_id: uuid.UUID, *, state: str = "active"
) -> PhoneNumber:
    pn = PhoneNumber(
        organization_id=org_id,
        e164=f"+1415555{uuid.uuid4().hex[:4]}",
        country_code="US",
        number_type="local",
        provider="twilio",
        provider_resource_id=f"PN-{uuid.uuid4()}",
        provisioning_state=state,
    )
    session.add(pn)
    await session.commit()
    return pn


async def test_internal_release_releases_number(
    client, async_session, voice_provider_mock, internal_secret_set
) -> None:
    org_id = uuid.uuid4()
    pn = await _seed_number(async_session, org_id)

    body = json.dumps(
        {"organization_id": str(org_id), "number_id": str(pn.id), "source": "dunning"}
    ).encode()
    resp = await client.post(
        "/internal/numbers/release", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["provisioning_state"] == "released"
    assert payload["released_at"] is not None
    voice_provider_mock.release_number.assert_awaited_once_with(
        pn.provider_resource_id
    )


async def test_internal_release_idempotent_when_already_released(
    client, async_session, voice_provider_mock, internal_secret_set
) -> None:
    org_id = uuid.uuid4()
    pn = await _seed_number(async_session, org_id, state="released")

    body = json.dumps(
        {"organization_id": str(org_id), "number_id": str(pn.id)}
    ).encode()
    resp = await client.post(
        "/internal/numbers/release", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provisioning_state"] == "released"
    voice_provider_mock.release_number.assert_not_awaited()


async def test_internal_release_404_on_org_mismatch(
    client, async_session, voice_provider_mock, internal_secret_set
) -> None:
    pn = await _seed_number(async_session, uuid.uuid4())

    body = json.dumps(
        {"organization_id": str(uuid.uuid4()), "number_id": str(pn.id)}
    ).encode()
    resp = await client.post(
        "/internal/numbers/release", content=body, headers=_signed(body)
    )
    assert resp.status_code == 404
    voice_provider_mock.release_number.assert_not_awaited()


async def test_internal_release_401_without_signature(
    client, async_session, voice_provider_mock, internal_secret_set
) -> None:
    pn = await _seed_number(async_session, uuid.uuid4())

    resp = await client.post(
        "/internal/numbers/release",
        json={"organization_id": str(pn.organization_id), "number_id": str(pn.id)},
    )
    assert resp.status_code == 401
    voice_provider_mock.release_number.assert_not_awaited()
