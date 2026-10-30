"""Tests for GET /emails/suppressions and DELETE /emails/suppressions/{address}."""

from __future__ import annotations

import uuid

from hailhq.core.compliance_gate import add_suppression
from hailhq.core.models import Suppression
from sqlalchemy import select


def _auth(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


async def _suppress(session, org_id, recipient: str, channel: str = "email") -> None:
    await add_suppression(
        session,
        organization_id=org_id,
        recipient=recipient,
        channel=channel,
        reason="unsubscribed",
        source="unsubscribe_link",
    )
    await session.commit()


async def test_list_suppressions_not_swallowed_by_email_id_route(
    client, org_and_key
) -> None:
    """GET /emails/suppressions must reach the list route, not GET /emails/{email_id}
    (which would 422 on a non-UUID id)."""
    _, _, plaintext = org_and_key
    resp = await client.get("/emails/suppressions", headers=_auth(plaintext))
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


async def test_list_suppressions_returns_org_email_rows(
    client, async_session, org_and_key
) -> None:
    org_id, _, plaintext = org_and_key
    await _suppress(async_session, org_id, "alice@example.com")
    await _suppress(async_session, org_id, "+14155551234", channel="sms")
    await _suppress(async_session, uuid.uuid4(), "bob@example.com")
    await _suppress(async_session, None, "trap@example.com")

    resp = await client.get("/emails/suppressions", headers=_auth(plaintext))
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    item = items[0]
    assert item["recipient"] == "alice@example.com"
    assert item["channel"] == "email"
    assert item["reason"] == "unsubscribed"
    assert item["source"] == "unsubscribe_link"
    assert item["created_at"]


async def test_list_suppressions_paginates(client, async_session, org_and_key) -> None:
    org_id, _, plaintext = org_and_key
    for i in range(3):
        await _suppress(async_session, org_id, f"user{i}@example.com")

    first = await client.get(
        "/emails/suppressions", params={"limit": 2}, headers=_auth(plaintext)
    )
    body = first.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"]

    second = await client.get(
        "/emails/suppressions",
        params={"limit": 2, "cursor": body["next_cursor"]},
        headers=_auth(plaintext),
    )
    rest = second.json()
    assert len(rest["items"]) == 1
    assert rest["next_cursor"] is None
    seen = {i["recipient"] for i in body["items"] + rest["items"]}
    assert seen == {f"user{i}@example.com" for i in range(3)}


async def test_delete_suppression_removes_row(
    client, async_session, org_and_key
) -> None:
    org_id, _, plaintext = org_and_key
    await _suppress(async_session, org_id, "alice@example.com")

    resp = await client.delete(
        "/emails/suppressions/alice@example.com", headers=_auth(plaintext)
    )
    assert resp.status_code == 204

    resp = await client.get("/emails/suppressions", headers=_auth(plaintext))
    assert resp.json()["items"] == []


async def test_delete_suppression_ignores_case(
    client, async_session, org_and_key
) -> None:
    org_id, _, plaintext = org_and_key
    await _suppress(async_session, org_id, "alice@example.com")

    resp = await client.delete(
        "/emails/suppressions/Alice@Example.COM", headers=_auth(plaintext)
    )
    assert resp.status_code == 204


async def test_delete_suppression_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.delete(
        "/emails/suppressions/nobody@example.com", headers=_auth(plaintext)
    )
    assert resp.status_code == 404


async def test_delete_never_removes_platform_wide_row(
    client, async_session, org_and_key
) -> None:
    _, _, plaintext = org_and_key
    await _suppress(async_session, None, "trap@example.com")

    resp = await client.delete(
        "/emails/suppressions/trap@example.com", headers=_auth(plaintext)
    )
    assert resp.status_code == 404

    rows = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "trap@example.com")
        )
    ).all()
    assert len(rows) == 1


async def test_delete_never_removes_other_org_row(
    client, async_session, org_and_key
) -> None:
    _, _, plaintext = org_and_key
    other_org = uuid.uuid4()
    await _suppress(async_session, other_org, "bob@example.com")

    resp = await client.delete(
        "/emails/suppressions/bob@example.com", headers=_auth(plaintext)
    )
    assert resp.status_code == 404

    rows = (
        await async_session.execute(
            select(Suppression).where(Suppression.organization_id == other_org)
        )
    ).all()
    assert len(rows) == 1


async def test_delete_leaves_all_channel_row(
    client, async_session, org_and_key
) -> None:
    org_id, _, plaintext = org_and_key
    await _suppress(async_session, org_id, "alice@example.com")
    await _suppress(async_session, org_id, "alice@example.com", channel="all")

    resp = await client.delete(
        "/emails/suppressions/alice@example.com", headers=_auth(plaintext)
    )
    assert resp.status_code == 204

    channels = (
        (
            await async_session.execute(
                select(Suppression.channel).where(
                    Suppression.recipient == "alice@example.com"
                )
            )
        )
        .scalars()
        .all()
    )
    assert channels == ["all"]
