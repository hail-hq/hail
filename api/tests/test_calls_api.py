"""Integration tests for the v1 calls API.

The conftest's ``async_session`` fixture installs the test sessionmaker
into ``hailhq.core.db._sessionmaker`` so ``session_scope()`` (used by
audit-log writes and the LiveKit-failure update path) talks to the test
database without any FastAPI dep override.
"""

from __future__ import annotations

from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import AsyncMock

from hailhq.core.models import (
    ApiKey,
    AuditLog,
    Call,
    CallEvent,
)
from hailhq.core.pool import CALL_META_FROM_POOL
from .conftest import insert_org_and_key

# --------------------------------------------------------------------------- #
# POST /calls
# --------------------------------------------------------------------------- #


async def test_post_calls_unauthenticated_returns_401(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
    )
    assert resp.status_code == 401


async def test_post_calls_invalid_body_returns_422(
    client: httpx.AsyncClient,
    org_and_key: tuple[str, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/calls",
        json={"to": "4155559999", "system_prompt": "hi"},  # not E.164
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_post_calls_rejects_prompt_and_llm_together(
    client: httpx.AsyncClient,
    org_and_key: tuple[str, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "llm": {
                "base_url": "https://example.invalid/v1",
                "api_key": "k",
                "model": "m",
            },
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    assert "mutually exclusive" in resp.text


async def test_post_calls_no_number_and_empty_pool_returns_503(
    client: httpx.AsyncClient,
    org_and_key: tuple[str, ApiKey, str],
) -> None:
    """When the org has no number and the shared pool is empty, expect 503.

    Old behavior was 422 — the resolver now falls back to a shared pool
    before giving up, so the failure mode is "pool exhausted" rather than
    "you have no number".
    """
    _, _, plain = org_and_key
    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
    assert "pool exhausted" in resp.json()["detail"]


async def test_post_calls_happy_path_201(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, api_key, plain = org_and_key
    pn = await add_phone_number(async_session, org_id, e164="+14155551234")

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "Be brief."},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "dialing"
    assert body["livekit_room"] == "hail-test-room"
    assert body["from_e164"] == pn.e164
    assert body["to_e164"] == "+14155559999"
    assert body["provider_call_sid"] == "PA_test_sid_1"
    assert resp.headers["location"] == f"/calls/{body['id']}"

    livekit_mock.create_room.assert_awaited_once()
    livekit_mock.dispatch_agent.assert_awaited_once()
    dispatch_kwargs = livekit_mock.dispatch_agent.await_args.kwargs
    assert dispatch_kwargs["agent_name"] == "hail-voicebot"
    assert dispatch_kwargs["metadata"]["call_id"] == body["id"]
    assert dispatch_kwargs["metadata"]["system_prompt"] == "Be brief."

    livekit_mock.create_sip_participant.assert_awaited_once()
    sip_kwargs = livekit_mock.create_sip_participant.await_args.kwargs
    assert sip_kwargs["to_e164"] == "+14155559999"
    assert sip_kwargs["from_e164"] == pn.e164
    assert sip_kwargs["participant_identity"] == f"caller-{body['id']}"

    # Audit log row written.
    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "call.create")
        )
    ).scalar_one()
    assert audit.api_key_id == api_key.id
    assert audit.payload["to"] == "+14155559999"

    # Exactly one call_events row (queued -> dialing).
    events = (await async_session.execute(select(CallEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].kind == "state_change"
    assert events[0].payload == {"from": "queued", "to": "dialing"}


async def test_post_calls_livekit_failure_marks_call_failed(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    livekit_mock.create_sip_participant.side_effect = RuntimeError("trunk down")

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 502
    assert resp.json()["detail"] == "call setup failed"

    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.status == "failed"
    assert call.end_reason == "sip_participant_failed"
    assert call.ended_at is not None

    events = (await async_session.execute(select(CallEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].payload == {
        "from": "queued",
        "to": "failed",
        "reason": "sip_participant_failed",
    }
    livekit_mock.delete_dispatch.assert_awaited_once_with(
        "AD_test_dispatch", "hail-test-room"
    )
    livekit_mock.delete_room.assert_awaited_once_with("hail-test-room")


# --------------------------------------------------------------------------- #
# Pool fallback
# --------------------------------------------------------------------------- #


async def test_post_calls_falls_back_to_pool_when_org_has_no_number(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """Org without a number gets a pool number; reservation is bound to the call."""
    _, _, plain = org_and_key
    pool_pn = await add_phone_number(
        async_session,
        organization_id=None,
        e164="+14155550100",
        is_pool=True,
    )

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["from_e164"] == pool_pn.e164

    # Call row metadata stamps the pool origin.
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.from_number_id == pool_pn.id
    assert call.metadata_[CALL_META_FROM_POOL] is True
    assert call.max_duration_seconds is not None  # snapshotted from settings

    # Pool row's reservation now points at this call.
    await async_session.refresh(pool_pn)
    assert pool_pn.reserved_call_id == call.id


async def test_post_calls_skips_pool_when_org_has_active_number(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """An org-owned active number wins over the pool; pool reservation untouched."""
    org_id, _, plain = org_and_key
    org_pn = await add_phone_number(async_session, org_id, e164="+14155551234")
    pool_pn = await add_phone_number(
        async_session,
        organization_id=None,
        e164="+14155550100",
        is_pool=True,
    )

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["from_e164"] == org_pn.e164
    assert CALL_META_FROM_POOL not in (
        (await async_session.execute(select(Call))).scalar_one().metadata_
    )

    await async_session.refresh(pool_pn)
    assert pool_pn.reserved_call_id is None


async def test_post_calls_pool_exhausted_returns_503(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """When the only pool number is already reserved, the second caller gets 503."""
    _, _, plain = org_and_key
    pool_pn = await add_phone_number(
        async_session,
        organization_id=None,
        e164="+14155550100",
        is_pool=True,
    )

    # Pre-reserve the only pool row by inserting a placeholder Call.
    first_resp = await client.post(
        "/calls",
        json={"to": "+14155559998", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert first_resp.status_code == 201
    await async_session.refresh(pool_pn)
    assert pool_pn.reserved_call_id is not None

    # Second caller — same org, no number, pool empty → 503.
    resp = await client.post(
        "/calls",
        json={"to": "+14155559997", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503
    assert "pool exhausted" in resp.json()["detail"]


async def test_post_calls_explicit_from_cannot_address_pool(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """A caller cannot use `from` to grab a pool number — it's not theirs."""
    _, _, plain = org_and_key
    pool_pn = await add_phone_number(
        async_session,
        organization_id=None,
        e164="+14155550100",
        is_pool=True,
    )

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "from": pool_pn.e164,
            "system_prompt": "hi",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    assert "not registered to this organization" in resp.json()["detail"]


async def test_post_calls_pool_release_on_dispatch_failure(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    """A failed dispatch must release the pool reservation it claimed."""
    _, _, plain = org_and_key
    pool_pn = await add_phone_number(
        async_session,
        organization_id=None,
        e164="+14155550100",
        is_pool=True,
    )
    livekit_mock.create_sip_participant.side_effect = RuntimeError("trunk down")

    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 502

    # Call exists and is failed.
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.status == "failed"
    # And the pool row is back to available — release fired in the failure path.
    await async_session.refresh(pool_pn)
    assert pool_pn.reserved_call_id is None


async def test_post_calls_uses_explicit_from_e164(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    # Two active numbers; "first by created_at" would pick #1, but we ask for #2.
    await add_phone_number(
        async_session,
        org_id,
        e164="+14155550001",
        provider_resource_id="PN_first",
    )
    chosen = await add_phone_number(
        async_session,
        org_id,
        e164="+14155550002",
        provider_resource_id="PN_second",
    )

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "from": chosen.e164,
            "system_prompt": "hi",
        },
        headers={"Authorization": f"Bearer {plain}"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["from_e164"] == chosen.e164

    sip_kwargs = livekit_mock.create_sip_participant.await_args.kwargs
    assert sip_kwargs["from_e164"] == chosen.e164


# --------------------------------------------------------------------------- #
# GET /calls/{id}
# --------------------------------------------------------------------------- #


async def test_get_call_by_id_returns_200_for_owner(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    create = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert create.status_code == 201
    call_id = create.json()["id"]

    resp = await client.get(
        f"/calls/{call_id}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == call_id


async def test_get_call_by_id_returns_404_for_other_org(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_a_id, _, plain_a = org_and_key
    await add_phone_number(async_session, org_a_id)

    create = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain_a}"},
    )
    call_id = create.json()["id"]

    # Second org with its own api key.
    _, _, plain_b = await insert_org_and_key(
        async_session, org_name="Beta", org_slug="beta"
    )

    resp = await client.get(
        f"/calls/{call_id}",
        headers={"Authorization": f"Bearer {plain_b}"},
    )
    assert resp.status_code == 404

    # Random unknown UUID also 404 (not 5xx).
    resp = await client.get(
        f"/calls/{uuid4()}",
        headers={"Authorization": f"Bearer {plain_a}"},
    )
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# GET /calls
# --------------------------------------------------------------------------- #


async def test_list_calls_returns_pagination_cursor(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    created_ids = []
    for _ in range(3):
        resp = await client.post(
            "/calls",
            json={"to": "+14155559999", "system_prompt": "hi"},
            headers={"Authorization": f"Bearer {plain}"},
        )
        assert resp.status_code == 201
        created_ids.append(resp.json()["id"])

    page1 = await client.get(
        "/calls?limit=2",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert page1.status_code == 200
    body1 = page1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"] is not None

    page2 = await client.get(
        f"/calls?limit=2&cursor={body1['next_cursor']}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert page2.status_code == 200
    body2 = page2.json()
    assert len(body2["items"]) == 1
    assert body2["next_cursor"] is None

    seen = [item["id"] for item in body1["items"]] + [
        item["id"] for item in body2["items"]
    ]
    assert sorted(seen) == sorted(created_ids)


async def test_list_calls_filters_by_status(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    livekit_mock: AsyncMock,
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    # First call: succeeds → status=dialing.
    r1 = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r1.status_code == 201

    # Second call: flip the mock to fail → status=failed.
    livekit_mock.create_sip_participant.side_effect = RuntimeError("nope")
    r2 = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r2.status_code == 502

    resp = await client.get(
        "/calls?status=dialing",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "dialing"

    resp = await client.get(
        "/calls?status=failed",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"
