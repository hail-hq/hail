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

from hailhq.core.models import (
    ApiKey,
    CallEvent,
)
from .conftest import insert_org_and_key
from .test_emails_api import _register_custom_verified, _send_email

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


async def _create_call_for_events(client: httpx.AsyncClient, plain: str) -> str:
    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
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


async def _make_second_org(
    session: AsyncSession,
) -> tuple[str, ApiKey, str]:
    return await insert_org_and_key(session, org_name="Beta", org_slug="beta")


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #


async def test_get_events_unauth_returns_401(client: httpx.AsyncClient) -> None:
    resp = await client.get("/events")
    assert resp.status_code == 401


async def test_get_events_returns_only_my_org_events(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_a_id, _, plain_a = org_and_key
    await add_phone_number(async_session, org_a_id)
    call_a = await _create_call_for_events(client, plain_a)

    # Wipe the synthetic queued->dialing state_change so we can count exactly.
    await async_session.execute(
        CallEvent.__table__.delete().where(CallEvent.call_id == call_a)
    )
    await async_session.commit()

    org_b_id, _, plain_b = await _make_second_org(async_session)
    await add_phone_number(
        async_session,
        org_b_id,
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
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)
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
    org_and_key: tuple[str, ApiKey, str],
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
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_a_id, _, plain_a = org_and_key
    await add_phone_number(async_session, org_a_id)
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
    org_and_key: tuple[str, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    resp = await client.get(
        f"/events?id=fax:{uuid4()}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"][0]["msg"]
    assert "unsupported resource type 'fax'" in detail
    assert "supported:" in detail
    assert "call" in detail
    assert "email" in detail


async def test_get_events_id_filter_malformed_returns_422(
    client: httpx.AsyncClient,
    org_and_key: tuple[str, ApiKey, str],
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    # Missing colon altogether.
    resp = await client.get("/events?id=nocolon", headers=headers)
    assert resp.status_code == 422
    assert "missing ':'" in resp.json()["detail"][0]["msg"]

    # Non-UUID after a valid type.
    resp = await client.get("/events?id=call:not-a-uuid", headers=headers)
    assert resp.status_code == 422
    assert "invalid uuid" in resp.json()["detail"][0]["msg"]

    # Bare colon — no type, no id.
    resp = await client.get("/events?id=:", headers=headers)
    assert resp.status_code == 422
    assert "missing resource type" in resp.json()["detail"][0]["msg"]

    # Empty after the colon.
    resp = await client.get("/events?id=call:", headers=headers)
    assert resp.status_code == 422
    assert "missing resource id" in resp.json()["detail"][0]["msg"]


async def test_get_events_kind_filter(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)
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
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)
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
    org_and_key: tuple[str, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)
    await _create_call_for_events(client, plain)

    resp = await client.get("/events", headers={"Authorization": f"Bearer {plain}"})
    assert resp.status_code == 200
    body = resp.json()
    # The contract: call_status is None for org-wide queries. The pydantic
    # default serializes the absent value as JSON null.
    assert body["call_status"] is None


async def test_stream_merges_email_events(
    client: httpx.AsyncClient, async_session: AsyncSession, add_phone_number
) -> None:
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await add_phone_number(async_session, org_id)
    call_id = await _create_call_for_events(client, plain)
    await _add_event(async_session, call_id, "call.queued", {})

    await _register_custom_verified(client, headers, domain="acme.com")
    email_id = (await _send_email(client, plain))["id"]

    r = await client.get("/events", headers=headers)
    assert r.status_code == 200
    items = r.json()["items"]
    sources = {i["source"] for i in items}
    assert sources == {"call", "email"}
    email_items = [i for i in items if i["source"] == "email"]
    assert email_items[0]["email_id"] == email_id
    assert email_items[0]["call_id"] is None
    # ascending (occurred_at, id) across both sources
    keys = [(i["occurred_at"], i["id"]) for i in items]
    assert keys == sorted(keys)


async def test_stream_email_id_filter(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    email_id = (await _send_email(client, plain))["id"]
    r = await client.get(
        "/events",
        params={"id": f"email:{email_id}"},
        headers=headers,
    )
    assert r.status_code == 200
    body = r.json()
    assert all(i["email_id"] == email_id for i in body["items"])
    # call_status is a call-only concept — must stay None when the id
    # resolves to an email.
    assert body["call_status"] is None

    # cross-org 404
    _, _, other = await insert_org_and_key(async_session)
    r2 = await client.get(
        "/events",
        params={"id": f"email:{email_id}"},
        headers={"Authorization": f"Bearer {other}"},
    )
    assert r2.status_code == 404


async def test_get_events_email_org_isolation(
    client: httpx.AsyncClient, async_session: AsyncSession
) -> None:
    """Org-wide GET /events must never leak another org's email events."""
    org_a_id, _, plain_a = await insert_org_and_key(async_session)
    headers_a = {"Authorization": f"Bearer {plain_a}"}
    await _register_custom_verified(client, headers_a, domain="acme.com")
    email_id = (await _send_email(client, plain_a))["id"]

    org_b_id, _, plain_b = await _make_second_org(async_session)
    headers_b = {"Authorization": f"Bearer {plain_b}"}

    resp = await client.get("/events", headers=headers_b)
    assert resp.status_code == 200
    items = resp.json()["items"]
    # Org B sent no email, so it must see no email-source items at all, and
    # in particular none carrying org A's email_id.
    assert not any(i["source"] == "email" for i in items)
    assert not any(i["email_id"] == email_id for i in items)


async def test_get_events_kind_filter_email_and_call(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    add_phone_number,
) -> None:
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await add_phone_number(async_session, org_id)
    call_id = await _create_call_for_events(client, plain)
    await _add_event(async_session, call_id, "call.queued", {})

    await _register_custom_verified(client, headers, domain="acme.com")
    email_id = (await _send_email(client, plain))["id"]

    resp = await client.get("/events?kind=sent", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "expected at least one 'sent' email event"
    assert all(i["kind"] == "sent" for i in items)
    assert all(i["source"] == "email" for i in items)
    assert all(i["email_id"] == email_id for i in items)

    resp = await client.get("/events?kind=call.queued", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items, "expected at least one 'call.queued' event"
    assert all(i["kind"] == "call.queued" for i in items)
    assert all(i["source"] == "call" for i in items)
    assert all(i["call_id"] == call_id for i in items)


async def test_stream_cursor_walk_crosses_sources(
    client: httpx.AsyncClient, async_session: AsyncSession, add_phone_number
) -> None:
    """Cursor pagination must not drop or duplicate events when a page
    boundary falls between a call event and an email event in the merged
    stream."""
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await add_phone_number(async_session, org_id)
    call_id = await _create_call_for_events(client, plain)

    await _register_custom_verified(client, headers, domain="acme.com")
    email_id = (await _send_email(client, plain))["id"]

    # Interleave sources on the timeline: call@t1, email@t2, call@t3, email@t4.
    from sqlalchemy import update

    from hailhq.core.models import EmailEvent

    t = lambda m: datetime(2026, 7, 1, 12, m, tzinfo=timezone.utc)  # noqa: E731
    await _add_event(async_session, call_id, "call.queued", {}, occurred_at=t(1))
    await async_session.execute(
        update(EmailEvent)
        .where(EmailEvent.email_id == email_id)
        .values(occurred_at=t(2))
    )
    await _add_event(async_session, call_id, "call.ringing", {}, occurred_at=t(3))
    async_session.add(
        EmailEvent(
            email_id=email_id,
            organization_id=org_id,
            kind="delivered",
            payload={},
            occurred_at=t(4),
        )
    )
    await async_session.commit()

    full = (
        await client.get("/events", params={"limit": 1000}, headers=headers)
    ).json()["items"]
    assert len(full) >= 4
    assert {i["source"] for i in full} == {"call", "email"}

    walked: list[dict] = []
    cursor = None
    pages = 0
    while True:
        params: dict = {"limit": 2}
        if cursor:
            params["cursor"] = cursor
        body = (await client.get("/events", params=params, headers=headers)).json()
        walked.extend(body["items"])
        cursor = body["next_cursor"]
        pages += 1
        if cursor is None:
            break
    assert pages >= 2
    assert [i["id"] for i in walked] == [i["id"] for i in full]  # no dup, no loss
