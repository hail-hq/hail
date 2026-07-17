"""Integration tests: agent velocity caps wired into POST /emails, /sms, /calls.

Mirrors test_compliance_gate_api.py's setup (verified custom domain for
email, dedicated numbers for sms/calls) but flags the org as agent-origin
and drives the velocity cap / kill switch added in agent_gate.py.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_caps import AGENT_OUTBOUND_DISABLED_FLAG
from hailhq.core.config import settings
from hailhq.core.models import ApiKey, Organization, PhoneNumber, PlatformFlag

from .conftest import insert_org_and_key  # noqa: F401


async def _register_custom_verified(
    client: httpx.AsyncClient,
    headers: dict,
    domain: str = "acme.com",
) -> str:
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": domain},
        headers=headers,
    )
    assert created.status_code == 201
    await client.post(f"/email-domains/{created.json()['id']}/verify", headers=headers)
    return created.json()["id"]


async def _seed_dedicated_number(
    session: AsyncSession, organization_id, e164: str
) -> None:
    session.add(
        PhoneNumber(
            organization_id=organization_id,
            e164=e164,
            country_code="US",
            number_type="local",
            provider_resource_id="PN_test",
            provisioning_state="active",
        )
    )
    await session.commit()


async def _flag_org_agent(db: AsyncSession, organization_id: uuid.UUID) -> None:
    db.add(Organization(id=organization_id, origin="agent"))
    await db.flush()


# --------------------------------------------------------------------------- #
# Email.
# --------------------------------------------------------------------------- #


async def test_agent_org_email_send_hits_hourly_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)
    await _flag_org_agent(async_session, org_id)
    monkeypatch.setattr(settings, "agent_email_per_hour", 2)

    payload = {
        "to": ["cap@example.com"],
        "subject": "hi",
        "body_text": "hi",
        "recipient_consent": True,
    }
    for _ in range(2):
        r = await client.post("/emails", json=payload, headers=headers)
        assert r.status_code == 201, r.text

    r = await client.post("/emails", json=payload, headers=headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "hour" in r.json()["detail"]


async def test_human_org_unaffected_by_agent_email_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _, plain = org_and_key  # no agent flag — origin defaults to 'human'
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)
    monkeypatch.setattr(settings, "agent_email_per_hour", 1)

    payload = {
        "to": ["h@example.com"],
        "subject": "hi",
        "body_text": "hi",
        "recipient_consent": True,
    }
    for _ in range(3):
        r = await client.post("/emails", json=payload, headers=headers)
        assert r.status_code == 201, r.text


async def test_kill_switch_blocks_email(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _flag_org_agent(async_session, org_id)
    async_session.add(PlatformFlag(key=AGENT_OUTBOUND_DISABLED_FLAG, value="true"))
    await async_session.flush()

    r = await client.post(
        "/emails",
        json={
            "to": ["k@example.com"],
            "subject": "hi",
            "body_text": "hi",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert r.status_code == 429
    assert "disabled" in r.json()["detail"]


async def test_agent_org_email_cc_bcc_fanout_hits_distinct_recipient_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single send's to+cc+bcc must all count toward the distinct-recipient
    cap — not just `to[0]` — otherwise a cc/bcc fan-out defeats the cap in
    one call."""
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)
    await _flag_org_agent(async_session, org_id)
    monkeypatch.setattr(settings, "agent_email_recipients_per_day", 3)

    payload = {
        "to": ["a@example.com"],
        "cc": ["b@example.com", "c@example.com"],
        "bcc": ["d@example.com"],
        "subject": "hi",
        "body_text": "hi",
        "recipient_consent": True,
    }
    r = await client.post("/emails", json=payload, headers=headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "recipient" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# SMS.
# --------------------------------------------------------------------------- #


async def test_agent_org_sms_send_hits_hourly_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _seed_dedicated_number(async_session, org_id, "+14155559999")
    await _flag_org_agent(async_session, org_id)
    monkeypatch.setattr(settings, "agent_sms_per_hour", 2)

    payload = {"to": "+14155551234", "body": "hi", "recipient_consent": True}
    for _ in range(2):
        r = await client.post("/sms", json=payload, headers=headers)
        assert r.status_code == 201, r.text

    r = await client.post("/sms", json=payload, headers=headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "hour" in r.json()["detail"]


async def test_kill_switch_blocks_sms(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _flag_org_agent(async_session, org_id)
    async_session.add(PlatformFlag(key=AGENT_OUTBOUND_DISABLED_FLAG, value="true"))
    await async_session.flush()

    r = await client.post(
        "/sms",
        json={"to": "+14155551234", "body": "hi", "recipient_consent": True},
        headers=headers,
    )
    assert r.status_code == 429
    assert "disabled" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# Calls.
# --------------------------------------------------------------------------- #


async def test_agent_org_call_send_hits_hourly_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    add_phone_number,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await add_phone_number(async_session, org_id, e164="+14155551234")
    await _flag_org_agent(async_session, org_id)
    monkeypatch.setattr(settings, "agent_voice_per_hour", 2)

    payload = {
        "to": "+14155559999",
        "system_prompt": "Be brief.",
        "recipient_consent": True,
    }
    for _ in range(2):
        r = await client.post("/calls", json=payload, headers=headers)
        assert r.status_code == 201, r.text

    r = await client.post("/calls", json=payload, headers=headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers
    assert "hour" in r.json()["detail"]


async def test_kill_switch_blocks_calls(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await add_phone_number(async_session, org_id, e164="+14155551234")
    await _flag_org_agent(async_session, org_id)
    async_session.add(PlatformFlag(key=AGENT_OUTBOUND_DISABLED_FLAG, value="true"))
    await async_session.flush()

    r = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi", "recipient_consent": True},
        headers=headers,
    )
    assert r.status_code == 429
    assert "disabled" in r.json()["detail"]
