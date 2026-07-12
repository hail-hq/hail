"""Tests for GET/PATCH /sms/sender-id."""

from __future__ import annotations


async def test_get_sender_id_defaults_to_hail(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.get(
        "/sms/sender-id", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.status_code == 200
    assert resp.json()["custom_sender_id"] is None
    assert resp.json()["effective_default"] == "HAIL"


async def test_patch_sender_id_sets_custom_value(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.patch(
        "/sms/sender-id",
        json={"custom_sender_id": "ACME"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    assert resp.json()["custom_sender_id"] == "ACME"

    resp = await client.get(
        "/sms/sender-id", headers={"Authorization": f"Bearer {plaintext}"}
    )
    assert resp.json()["custom_sender_id"] == "ACME"


async def test_patch_sender_id_rejects_too_long(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.patch(
        "/sms/sender-id",
        json={
            "custom_sender_id": "WAYTOOLONGID"
        },  # 12 chars, over the 11-char GSM limit
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422


async def test_patch_sender_id_rejects_non_alphanumeric(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    resp = await client.patch(
        "/sms/sender-id",
        json={"custom_sender_id": "AC-ME!"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 422


async def test_patch_sender_id_clears_with_null(client, org_and_key) -> None:
    _, _, plaintext = org_and_key
    await client.patch(
        "/sms/sender-id",
        json={"custom_sender_id": "ACME"},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    resp = await client.patch(
        "/sms/sender-id",
        json={"custom_sender_id": None},
        headers={"Authorization": f"Bearer {plaintext}"},
    )
    assert resp.status_code == 200
    assert resp.json()["custom_sender_id"] is None
