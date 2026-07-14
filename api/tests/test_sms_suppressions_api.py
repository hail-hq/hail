"""Tests for GET /sms/suppressions and DELETE /sms/suppressions/{number}."""

from __future__ import annotations


async def test_list_suppressions_empty(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(
        "/sms/suppressions", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    assert resp.json() == {"items": [], "next_cursor": None}


async def test_list_suppressions_returns_org_rows(
    client, async_session, org_and_key
) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="opted out",
        source="stop_keyword",
    )
    await async_session.commit()

    resp = await client.get(
        "/sms/suppressions", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["recipient"] == "+14155551234"


async def test_delete_suppression_removes_row(
    client, async_session, org_and_key
) -> None:
    from hailhq.core.compliance_gate import add_suppression

    org_id, _, plaintext = org_and_key
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155551234",
        channel="sms",
        reason="opted out",
        source="stop_keyword",
    )
    await async_session.commit()

    resp = await client.delete(
        "/sms/suppressions/+14155551234",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 204

    resp = await client.get(
        "/sms/suppressions", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.json()["items"] == []


async def test_delete_suppression_not_found(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.delete(
        "/sms/suppressions/+14155559999",
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 404
