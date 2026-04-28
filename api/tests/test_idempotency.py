"""Tests for the ``POST /calls`` idempotency layer.

Exercises the dependency wired in ``api/hailhq/api/idempotency.py`` plus the
route's replay short-circuit / store-on-success / store-on-502 behaviour.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import generate_key
from hailhq.api.idempotency import (
    _IN_FLIGHT_STATUS,
    _storage_key,
    hash_request_body,
)
from hailhq.core.models import (
    ApiKey,
    AuditLog,
    IdempotencyKey,
    Organization,
)

# --------------------------------------------------------------------------- #
# Pure-function tests
# --------------------------------------------------------------------------- #


def test_hash_request_body_is_order_independent() -> None:
    a = hash_request_body({"to": "+14155559999", "system_prompt": "hi"})
    b = hash_request_body({"system_prompt": "hi", "to": "+14155559999"})
    assert a == b


def test_hash_request_body_changes_on_body_change() -> None:
    a = hash_request_body({"to": "+14155559999", "system_prompt": "hi"})
    b = hash_request_body({"to": "+14155558888", "system_prompt": "hi"})
    assert a != b


# --------------------------------------------------------------------------- #
# HTTP-level tests
# --------------------------------------------------------------------------- #


async def test_post_calls_no_idempotency_header_normal_flow(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    """No header → no row in idempotency_keys."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    assert "idempotency-replay" not in {h.lower() for h in resp.headers.keys()}

    rows = (await async_session.execute(select(IdempotencyKey))).scalars().all()
    assert rows == []


async def test_post_calls_idempotency_first_request_inserts_and_runs(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """First request stores final status + body; replay header NOT set."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={
            "Authorization": f"Bearer {plain}",
            "Idempotency-Key": "first-key",
        },
    )
    assert resp.status_code == 201
    assert "idempotency-replay" not in {h.lower() for h in resp.headers.keys()}

    livekit_mock.dispatch_agent.assert_awaited_once()

    row = (
        await async_session.execute(
            select(IdempotencyKey).where(
                IdempotencyKey.key == _storage_key(org.id, "first-key")
            )
        )
    ).scalar_one()
    assert row.response_status == 201
    assert row.organization_id == org.id
    assert row.response_body["id"] == resp.json()["id"]
    assert row.response_body["status"] == "dialing"


async def test_post_calls_idempotency_replay_returns_cached(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """Same key + same body → second response replays first; LiveKit ran once."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    headers = {
        "Authorization": f"Bearer {plain}",
        "Idempotency-Key": "rerun-me",
    }
    body = {"to": "+14155559999", "system_prompt": "hi"}

    first = await client.post("/calls", json=body, headers=headers)
    assert first.status_code == 201
    assert "idempotency-replay" not in {h.lower() for h in first.headers.keys()}

    second = await client.post("/calls", json=body, headers=headers)
    assert second.status_code == 201
    assert second.headers.get("idempotency-replay") == "true"

    # Same id (the call wasn't re-created).
    assert second.json()["id"] == first.json()["id"]
    # Same Location header.
    assert second.headers["location"] == f"/calls/{first.json()['id']}"

    # The LiveKit pipeline ran exactly once across both requests.
    assert livekit_mock.create_room.await_count == 1
    assert livekit_mock.dispatch_agent.await_count == 1
    assert livekit_mock.create_sip_participant.await_count == 1


async def test_post_calls_idempotency_different_body_returns_409(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    """Same key, different body → 409, not a silent overwrite."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    headers = {
        "Authorization": f"Bearer {plain}",
        "Idempotency-Key": "ambiguous-key",
    }

    first = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers=headers,
    )
    assert first.status_code == 201

    second = await client.post(
        "/calls",
        json={"to": "+14155558888", "system_prompt": "hi"},
        headers=headers,
    )
    assert second.status_code == 409
    assert "different" in second.json()["detail"].lower()


async def test_post_calls_idempotency_in_flight_returns_409(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    """A row stuck at the in-flight sentinel surfaces 'still processing' 409."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    body = {"to": "+14155559999", "system_prompt": "hi"}
    request_hash = hash_request_body(body)

    # Manually plant an in-flight row to simulate a concurrent worker.
    async_session.add(
        IdempotencyKey(
            key=_storage_key(org.id, "in-flight-key"),
            organization_id=org.id,
            request_hash=request_hash,
            response_status=_IN_FLIGHT_STATUS,
            response_body={},
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
    )
    await async_session.commit()

    resp = await client.post(
        "/calls",
        json=body,
        headers={
            "Authorization": f"Bearer {plain}",
            "Idempotency-Key": "in-flight-key",
        },
    )
    assert resp.status_code == 409
    assert "still processing" in resp.json()["detail"]


async def test_post_calls_idempotency_isolated_per_org(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    """Different orgs reusing the same supplied key both succeed (no collision)."""
    org_a, _, plain_a = org_and_key
    await add_phone_number(async_session, org_a.id, e164="+14155551001")

    # Provision a second org with its own api key + active number.
    org_b = Organization(name="Beta", slug="beta")
    async_session.add(org_b)
    await async_session.flush()
    plain_b, prefix_b, hex_b = generate_key()
    async_session.add(
        ApiKey(
            organization_id=org_b.id,
            name="b-key",
            key_prefix=prefix_b,
            key_hash=hex_b,
        )
    )
    await async_session.commit()
    await add_phone_number(
        async_session,
        org_b.id,
        e164="+14155552002",
        provider_resource_id="PN_b",
    )

    body = {"to": "+14155559999", "system_prompt": "hi"}
    shared_key = "shared-key-across-orgs"

    a = await client.post(
        "/calls",
        json=body,
        headers={
            "Authorization": f"Bearer {plain_a}",
            "Idempotency-Key": shared_key,
        },
    )
    b = await client.post(
        "/calls",
        json=body,
        headers={
            "Authorization": f"Bearer {plain_b}",
            "Idempotency-Key": shared_key,
        },
    )

    assert a.status_code == 201
    assert b.status_code == 201
    assert a.json()["id"] != b.json()["id"]

    rows = (await async_session.execute(select(IdempotencyKey))).scalars().all()
    keys = {r.key for r in rows}
    assert keys == {
        _storage_key(org_a.id, shared_key),
        _storage_key(org_b.id, shared_key),
    }


async def test_post_calls_replay_emits_audit_log(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    """A replay re-emits a 'call.create.replayed' audit row."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    headers = {
        "Authorization": f"Bearer {plain}",
        "Idempotency-Key": "replayed-audit-key",
    }
    body = {"to": "+14155559999", "system_prompt": "hi"}

    await client.post("/calls", json=body, headers=headers)
    await client.post("/calls", json=body, headers=headers)

    actions = [
        row.action
        for row in (await async_session.execute(select(AuditLog))).scalars().all()
    ]
    assert "call.create" in actions
    assert "call.create.replayed" in actions


async def test_post_calls_idempotency_caches_502_failure(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """A LiveKit-failure 502 is cached so retries replay it (Stripe-style)."""
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)

    livekit_mock.create_sip_participant.side_effect = RuntimeError("trunk down")

    headers = {
        "Authorization": f"Bearer {plain}",
        "Idempotency-Key": "fail-key",
    }
    body = {"to": "+14155559999", "system_prompt": "hi"}

    first = await client.post("/calls", json=body, headers=headers)
    assert first.status_code == 502

    second = await client.post("/calls", json=body, headers=headers)
    assert second.status_code == 502
    assert second.headers.get("idempotency-replay") == "true"

    # The dispatch attempt happened exactly once even though we retried.
    assert livekit_mock.create_sip_participant.await_count == 1
