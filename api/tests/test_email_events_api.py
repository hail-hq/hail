"""GET /emails/{id}/events — per-email lifecycle timeline."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.models import EmailEvent
from .conftest import insert_org_and_key
from .test_emails_api import _register_custom_verified, _send_email


async def test_events_ordered_and_scoped(client, async_session: AsyncSession):
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    email = await _send_email(client, plain)

    async_session.add(
        EmailEvent(
            email_id=email["id"],
            organization_id=org_id,
            kind="delivered",
            payload={"smtp_response": "250 OK"},
            occurred_at=datetime(2026, 7, 1, 12, 0, 3, tzinfo=timezone.utc),
        )
    )
    await async_session.commit()

    r = await client.get(
        f"/emails/{email['id']}/events",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200
    kinds = [e["kind"] for e in r.json()["items"]]
    assert kinds == ["sent", "delivered"] or kinds == ["delivered", "sent"]
    # ascending by occurred_at:
    times = [e["occurred_at"] for e in r.json()["items"]]
    assert times == sorted(times)

    # cross-org → 404
    _, _, other_plain = await insert_org_and_key(async_session)
    r2 = await client.get(
        f"/emails/{email['id']}/events",
        headers={"Authorization": f"Bearer {other_plain}"},
    )
    assert r2.status_code == 404


async def test_events_cursor_pagination(client, async_session: AsyncSession):
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    email = await _send_email(client, plain)

    for i, kind in enumerate(["delivered", "opened", "clicked"], start=1):
        async_session.add(
            EmailEvent(
                email_id=email["id"],
                organization_id=org_id,
                kind=kind,
                payload={},
                occurred_at=datetime(2026, 7, 1, 12, i, tzinfo=timezone.utc),
            )
        )
    await async_session.commit()

    # 4 events total (synthetic sent + 3 seeded); walk in pages of 2.
    r1 = await client.get(
        f"/emails/{email['id']}/events", params={"limit": 2}, headers=headers
    )
    assert r1.status_code == 200
    body1 = r1.json()
    assert len(body1["items"]) == 2
    assert body1["next_cursor"]

    r2 = await client.get(
        f"/emails/{email['id']}/events",
        params={"limit": 2, "cursor": body1["next_cursor"]},
        headers=headers,
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert len(body2["items"]) == 2
    assert body2["next_cursor"] is None

    walked = [e["id"] for e in body1["items"] + body2["items"]]
    full = await client.get(f"/emails/{email['id']}/events", headers=headers)
    assert walked == [e["id"] for e in full.json()["items"]]  # no dup, no loss
    assert full.json()["next_cursor"] is None

    # bounds
    r = await client.get(
        f"/emails/{email['id']}/events", params={"limit": 0}, headers=headers
    )
    assert r.status_code == 422
    r = await client.get(
        f"/emails/{email['id']}/events", params={"limit": 1001}, headers=headers
    )
    assert r.status_code == 422
    r = await client.get(
        f"/emails/{email['id']}/events",
        params={"cursor": "not-a-cursor"},
        headers=headers,
    )
    assert r.status_code == 400


async def test_get_email_exposes_last_event_at(client, async_session):
    _, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    email = await _send_email(client, plain)
    r = await client.get(
        f"/emails/{email['id']}",
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert r.status_code == 200
    # The synthetic 'sent' event exists, so last_event_at is populated.
    assert r.json()["last_event_at"] is not None


async def test_events_same_timestamp_tie_breaks_by_id(
    client, async_session: AsyncSession
):
    org_id, _, plain = await insert_org_and_key(async_session)
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    email = await _send_email(client, plain)

    ts = datetime(2026, 7, 1, 12, 0, 3, tzinfo=timezone.utc)
    for kind in ("delivered", "opened"):
        async_session.add(
            EmailEvent(
                email_id=email["id"],
                organization_id=org_id,
                kind=kind,
                payload={},
                occurred_at=ts,
            )
        )
    await async_session.commit()

    r = await client.get(f"/emails/{email['id']}/events", headers=headers)
    assert r.status_code == 200
    tied = [
        e
        for e in r.json()["items"]
        if e["occurred_at"].startswith("2026-07-01T12:00:03")
    ]
    assert len(tied) == 2
    assert [e["id"] for e in tied] == sorted(e["id"] for e in tied)
