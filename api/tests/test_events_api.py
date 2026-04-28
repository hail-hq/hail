"""Integration tests for the v1 events stream API (``GET /events``).

The endpoint is the org-scoped successor to the deleted
``GET /calls/{call_id}/events``. It supports an optional typed ``id``
query filter (``<type>:<uuid>``, e.g. ``call:abc-...``; 404 on unknown /
cross-org, 422 on malformed / unsupported type), an optional ``kind``
filter, and the same ``cursor`` / ``limit`` cursor pagination shape.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.api.auth import generate_key
from hailhq.core.models import (
    ApiKey,
    CallEvent,
    Organization,
)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _create_call_for_events(client: httpx.AsyncClient, plain: str) -> str:
    resp = await client.post(
        "/calls",
        json={"to": "+14155559999", "system_prompt": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201
    return resp.json()["id"]


async def _add_event(
    session: AsyncSession,
    call_id: str,
    kind: str,
    payload: dict,
    occurred_at: datetime | None = None,
) -> CallEvent:
    ev = CallEvent(call_id=call_id, kind=kind, payload=payload)
    if occurred_at is not None:
        ev.occurred_at = occurred_at
    session.add(ev)
    await session.commit()
    await session.refresh(ev)
    return ev


async def _make_second_org(session: AsyncSession) -> tuple[Organization, ApiKey, str]:
    org = Organization(name="Beta", slug="beta")
    session.add(org)
    await session.flush()
    plain, prefix, hex_digest = generate_key()
    api_key = ApiKey(
        organization_id=org.id,
        name="b-key",
        key_prefix=prefix,
        key_hash=hex_digest,
    )
    session.add(api_key)
    await session.commit()
    await session.refresh(api_key)
    return org, api_key, plain


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_get_events_unauth_returns_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/events")
    assert resp.status_code == 401


async def test_get_events_returns_only_my_org_events(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    org_a, _, plain_a = org_and_key
    await add_phone_number(async_session, org_a.id)
    call_a = await _create_call_for_events(client, plain_a)

    # Wipe the synthetic queued->dialing state_change so we can count exactly.
    await async_session.execute(
        CallEvent.__table__.delete().where(CallEvent.call_id == call_a)
    )
    await async_session.commit()

    org_b, _, plain_b = await _make_second_org(async_session)
    await add_phone_number(
        async_session,
        org_b.id,
        e164="+14155550002",
        provider_resource_id="PN_b",
    )
    call_b = await _create_call_for_events(client, plain_b)
    await async_session.execute(
        CallEvent.__table__.delete().where(CallEvent.call_id == call_b)
    )
    await async_session.commit()

    base = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    await _add_event(async_session, call_a, "agent_turn", {"text": "alpha-1"}, base)
    await _add_event(
        async_session,
        call_a,
        "agent_turn",
        {"text": "alpha-2"},
        base + timedelta(seconds=1),
    )
    await _add_event(
        async_session,
        call_b,
        "agent_turn",
        {"text": "beta-1"},
        base + timedelta(seconds=2),
    )

    resp = await client.get("/events", headers={"Authorization": f"Bearer {plain_a}"})
    assert resp.status_code == 200
    items = resp.json()["items"]
    texts = sorted(e["payload"]["text"] for e in items)
    assert texts == ["alpha-1", "alpha-2"]


async def test_get_events_id_filter_call_resolves_to_call(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)
    call_id = await _create_call_for_events(client, plain)

    base = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    await _add_event(async_session, call_id, "agent_turn", {"text": "hi"}, base)

    resp = await client.get(
        f"/events?id=call:{call_id}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert all(item["call_id"] == call_id for item in body["items"])
    # call_status populated when id resolves to a call. Right after POST the
    # row sits in `dialing`.
    assert body["call_status"] == "dialing"


async def test_get_events_id_filter_unknown_call_returns_404(
    client: httpx.AsyncClient,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    resp = await client.get(
        f"/events?id=call:{uuid4()}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 404


async def test_get_events_id_filter_other_org_call_returns_404(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    org_a, _, plain_a = org_and_key
    await add_phone_number(async_session, org_a.id)
    call_a = await _create_call_for_events(client, plain_a)

    _, _, plain_b = await _make_second_org(async_session)

    # plain_b asks for a call that exists, but in another org → 404, NOT 200
    # with empty items (that would leak existence).
    resp = await client.get(
        f"/events?id=call:{call_a}",
        headers={"Authorization": f"Bearer {plain_b}"},
    )
    assert resp.status_code == 404


async def test_get_events_id_filter_unsupported_type_returns_422(
    client: httpx.AsyncClient,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    resp = await client.get(
        f"/events?id=sms:{uuid4()}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "unsupported resource type 'sms'" in detail
    assert "supported: [call]" in detail


async def test_get_events_id_filter_malformed_returns_422(
    client: httpx.AsyncClient,
    org_and_key: tuple[Organization, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    # Missing colon altogether.
    resp = await client.get("/events?id=nocolon", headers=headers)
    assert resp.status_code == 422
    assert "missing ':'" in resp.json()["detail"]

    # Non-UUID after a valid type.
    resp = await client.get("/events?id=call:not-a-uuid", headers=headers)
    assert resp.status_code == 422
    assert "invalid uuid" in resp.json()["detail"]

    # Bare colon — no type, no id.
    resp = await client.get("/events?id=:", headers=headers)
    assert resp.status_code == 422
    assert "missing resource type" in resp.json()["detail"]

    # Empty after the colon.
    resp = await client.get("/events?id=call:", headers=headers)
    assert resp.status_code == 422
    assert "missing resource id" in resp.json()["detail"]


async def test_get_events_kind_filter(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)
    call_id = await _create_call_for_events(client, plain)

    base = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    await _add_event(async_session, call_id, "agent_turn", {"text": "a"}, base)
    await _add_event(
        async_session,
        call_id,
        "user_turn",
        {"text": "b"},
        base + timedelta(seconds=1),
    )
    await _add_event(
        async_session,
        call_id,
        "agent_turn",
        {"text": "c"},
        base + timedelta(seconds=2),
    )

    resp = await client.get(
        "/events?kind=agent_turn",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert {e["kind"] for e in items} == {"agent_turn"}
    # 2 from the manual inserts (the synthetic state_change is filtered out).
    assert len(items) == 2


async def test_get_events_chronological_order_and_pagination(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)
    call_a = await _create_call_for_events(client, plain)
    call_b = await _create_call_for_events(client, plain)

    # Wipe synthetic state_changes so the count is exact.
    await async_session.execute(CallEvent.__table__.delete())
    await async_session.commit()

    base = datetime(2026, 4, 22, 12, 0, 0, tzinfo=timezone.utc)
    # 5 events spread across the 2 calls, interleaved in time.
    rows = [
        (call_a, "agent_turn", {"text": "a-1"}, base),
        (call_b, "agent_turn", {"text": "b-1"}, base + timedelta(seconds=1)),
        (call_a, "agent_turn", {"text": "a-2"}, base + timedelta(seconds=2)),
        (call_b, "agent_turn", {"text": "b-2"}, base + timedelta(seconds=3)),
        (call_a, "agent_turn", {"text": "a-3"}, base + timedelta(seconds=4)),
    ]
    for cid, kind, payload, ts in rows:
        await _add_event(async_session, cid, kind, payload, ts)

    seen_ids: list[str] = []
    seen_texts: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        url = "/events?limit=2"
        if cursor is not None:
            url += f"&cursor={cursor}"
        resp = await client.get(url, headers={"Authorization": f"Bearer {plain}"})
        assert resp.status_code == 200
        body = resp.json()
        seen_ids.extend(item["id"] for item in body["items"])
        seen_texts.extend(item["payload"]["text"] for item in body["items"])
        pages += 1
        cursor = body["next_cursor"]
        if cursor is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert len(seen_ids) == 5
    assert len(set(seen_ids)) == 5  # no dupes
    # ASC by occurred_at — must come back in insertion order.
    assert seen_texts == ["a-1", "b-1", "a-2", "b-2", "a-3"]


async def test_get_events_org_wide_omits_call_status(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[Organization, ApiKey, str],
    add_phone_number,
) -> None:
    org, _, plain = org_and_key
    await add_phone_number(async_session, org.id)
    await _create_call_for_events(client, plain)

    resp = await client.get("/events", headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 200
    body = resp.json()
    # The contract: call_status is None for org-wide queries. The pydantic
    # default serializes the absent value as JSON null.
    assert body["call_status"] is None
