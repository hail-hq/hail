"""Tests for the org-wide webhook subscription CRUD surface."""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.secret_cipher import SecretCipher, generate_key
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture(autouse=True)
def webhook_secret_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a real Fernet key so encrypt/decrypt round-trips in the route."""
    key = generate_key()
    monkeypatch.setattr(settings, "hail_webhook_secret_key", key)
    return key


@pytest.fixture()
async def auth_headers(async_session: AsyncSession) -> dict[str, str]:
    """Mint a fresh org + key and return the Authorization header for it."""
    from .conftest import insert_org_and_key

    _, _, plain = await insert_org_and_key(async_session)
    return {"Authorization": f"Bearer {plain}"}


@pytest.mark.asyncio
async def test_create_returns_plaintext_secret_once(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
    webhook_secret_key: str,
):
    resp = await client.post(
        "/webhooks",
        json={
            "target_url": "https://hooks.example.com/ingest",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["secret"].startswith("whs_")
    sub_id = body["id"]

    # Subsequent GET must NOT echo the secret.
    g = await client.get(f"/webhooks/{sub_id}", headers=auth_headers)
    assert g.status_code == 200
    assert g.json().get("secret") is None

    # The column stores ciphertext that decrypts back to the plaintext.
    sub = (
        await async_session.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
        )
    ).scalar_one()
    assert sub.secret_encrypted != body["secret"]
    assert (
        SecretCipher(webhook_secret_key).decrypt(sub.secret_encrypted) == body["secret"]
    )


@pytest.mark.asyncio
async def test_create_rejects_empty_event_types(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    resp = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/h", "event_types": []},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_paginates(client: httpx.AsyncClient, auth_headers: dict[str, str]):
    for i in range(3):
        await client.post(
            "/webhooks",
            json={
                "target_url": f"https://example.com/h{i}",
                "event_types": ["email.received"],
            },
            headers=auth_headers,
        )
    r = await client.get("/webhooks?limit=2", headers=auth_headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None


@pytest.mark.asyncio
async def test_rotate_secret_returns_new_value(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
    webhook_secret_key: str,
):
    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    first_secret = create.json()["secret"]

    rot = await client.post(f"/webhooks/{sub_id}/rotate-secret", headers=auth_headers)
    assert rot.status_code == 200
    new_secret = rot.json()["secret"]
    assert new_secret != first_secret
    assert new_secret.startswith("whs_")

    # The stored ciphertext now decrypts to the rotated plaintext.
    async_session.expire_all()
    sub = (
        await async_session.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
        )
    ).scalar_one()
    assert SecretCipher(webhook_secret_key).decrypt(sub.secret_encrypted) == new_secret


@pytest.mark.asyncio
async def test_patch_disables(client: httpx.AsyncClient, auth_headers: dict[str, str]):
    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    p = await client.patch(
        f"/webhooks/{sub_id}",
        json={"status": "disabled"},
        headers=auth_headers,
    )
    assert p.status_code == 200
    assert p.json()["status"] == "disabled"


@pytest.mark.asyncio
async def test_create_stamps_created_and_updated_at_alike(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
):
    """A new subscription has updated_at == created_at. This catches one of
    the two stamps going missing; it cannot tell now() from
    statement_timestamp() when both are missing."""
    create = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/h", "event_types": ["call.analyzed"]},
        headers=auth_headers,
    )
    created = (
        await async_session.execute(
            select(WebhookSubscription).where(
                WebhookSubscription.id == create.json()["id"]
            )
        )
    ).scalar_one()
    assert created.updated_at == created.created_at


@pytest.mark.asyncio
async def test_patch_and_rotation_bump_updated_at(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
):
    """call.analyzed sends only calls that ended after the subscription was
    created or last changed (hailhq.core.call_analysis), so every change
    bumps updated_at. (The test cannot tell now() from statement_timestamp():
    both fall inside the request.)"""
    create = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/h", "event_types": ["call.completed"]},
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    for change in (
        lambda: client.patch(
            f"/webhooks/{sub_id}",
            json={"event_types": ["call.completed", "call.analyzed"]},
            headers=auth_headers,
        ),
        lambda: client.post(f"/webhooks/{sub_id}/rotate-secret", headers=auth_headers),
    ):
        await async_session.execute(
            update(WebhookSubscription)
            .where(WebhookSubscription.id == sub_id)
            .values(updated_at=func.now() - timedelta(days=1))
        )
        await async_session.commit()
        before = await async_session.scalar(select(func.statement_timestamp()))
        await async_session.commit()
        resp = await change()
        assert resp.status_code == 200
        async_session.expire_all()
        updated_at = await async_session.scalar(
            select(WebhookSubscription.updated_at).where(
                WebhookSubscription.id == sub_id
            )
        )
        after = await async_session.scalar(select(func.statement_timestamp()))
        await async_session.commit()
        assert before <= updated_at <= after


@pytest.mark.asyncio
async def test_delete_removes(client: httpx.AsyncClient, auth_headers: dict[str, str]):
    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    d = await client.delete(f"/webhooks/{sub_id}", headers=auth_headers)
    assert d.status_code == 204
    g = await client.get(f"/webhooks/{sub_id}", headers=auth_headers)
    assert g.status_code == 404


@pytest.mark.asyncio
async def test_cross_org_isolation(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
):
    """Two orgs share the API; their subscriptions must not be visible to each other."""
    from .conftest import insert_org_and_key

    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]

    _, _, plain_b = await insert_org_and_key(async_session)
    other_headers = {"Authorization": f"Bearer {plain_b}"}

    g = await client.get(f"/webhooks/{sub_id}", headers=other_headers)
    assert g.status_code == 404


@pytest.mark.asyncio
async def test_deliveries_empty_on_fresh_subscription(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    d = await client.get(f"/webhooks/{sub_id}/deliveries", headers=auth_headers)
    assert d.status_code == 200
    assert d.json()["items"] == []


@pytest.mark.asyncio
async def test_create_rejects_non_https(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    resp = await client.post(
        "/webhooks",
        json={
            "target_url": "http://hooks.example.com/x",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "https" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_rejects_private_target(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    resp = await client.post(
        "/webhooks",
        json={
            "target_url": "https://169.254.169.254/meta",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "private" in resp.text.lower()


@pytest.mark.asyncio
async def test_patch_rejects_non_https_target_url(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    create = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/h", "event_types": ["email.received"]},
        headers=auth_headers,
    )
    sub_id = create.json()["id"]
    resp = await client.patch(
        f"/webhooks/{sub_id}",
        json={"target_url": "http://evil.com/steal"},
        headers=auth_headers,
    )
    assert resp.status_code == 422
    assert "https" in resp.text.lower()


@pytest.mark.asyncio
async def test_redeliver_resets_dead_row_to_pending(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
):
    """Spec §9: redeliver replays a 'dead' row — status back to pending, attempt 0."""
    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    sub_id = create.json()["id"]
    org_id = create.json()["organization_id"]

    delivery = WebhookDelivery(
        subscription_id=UUID(sub_id),
        event_type="email.received",
        event_id=uuid4(),
        payload={"organization_id": org_id, "data": {"id": "x"}},
        status="dead",
        attempt=7,
    )
    async_session.add(delivery)
    await async_session.commit()
    delivery_id = delivery.id

    resp = await client.post(
        f"/webhooks/{sub_id}/deliveries/{delivery_id}/redeliver",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["attempt"] == 0

    async_session.expire_all()
    await async_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt == 0


# --------------------------------------------------------------------------- #
# Audit logging on mutations
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_subscription_writes_audit_log(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
):
    from hailhq.core.models import AuditLog

    resp = await client.post(
        "/webhooks",
        json={
            "target_url": "https://hooks.example.com/audit",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    sub_id = resp.json()["id"]

    row = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "webhook.create")
        )
    ).scalar_one()
    assert row.resource_type == "webhook_subscription"
    assert str(row.resource_id) == sub_id
    assert row.payload == {"target_url": "https://hooks.example.com/audit"}


@pytest.mark.asyncio
async def test_webhook_mutations_write_audit_log(
    client: httpx.AsyncClient,
    auth_headers: dict[str, str],
    async_session: AsyncSession,
):
    """Every mutating handler writes one audit_log row — and never the secret."""
    from hailhq.core.models import AuditLog

    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://hooks.example.com/h",
            "event_types": ["email.received"],
        },
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    sub_id = create.json()["id"]
    org_id = create.json()["organization_id"]

    p = await client.patch(
        f"/webhooks/{sub_id}", json={"status": "disabled"}, headers=auth_headers
    )
    assert p.status_code == 200

    rot = await client.post(f"/webhooks/{sub_id}/rotate-secret", headers=auth_headers)
    assert rot.status_code == 200
    new_secret = rot.json()["secret"]

    delivery = WebhookDelivery(
        subscription_id=UUID(sub_id),
        event_type="email.received",
        event_id=uuid4(),
        payload={"organization_id": org_id, "data": {"id": "x"}},
        status="dead",
        attempt=7,
    )
    async_session.add(delivery)
    await async_session.commit()
    delivery_id = delivery.id

    rd = await client.post(
        f"/webhooks/{sub_id}/deliveries/{delivery_id}/redeliver",
        headers=auth_headers,
    )
    assert rd.status_code == 200, rd.text

    d = await client.delete(f"/webhooks/{sub_id}", headers=auth_headers)
    assert d.status_code == 204

    rows = (
        (
            await async_session.execute(
                select(AuditLog)
                .where(AuditLog.resource_type == "webhook_subscription")
                .order_by(AuditLog.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert [r.action for r in rows] == [
        "webhook.create",
        "webhook.patch",
        "webhook.rotate_secret",
        "webhook.redeliver",
        "webhook.delete",
    ]
    assert all(str(r.resource_id) == sub_id for r in rows)

    by_action = {r.action: r for r in rows}
    assert by_action["webhook.patch"].payload["status"] == "disabled"
    assert by_action["webhook.rotate_secret"].payload == {}
    assert by_action["webhook.redeliver"].payload == {"delivery_id": str(delivery_id)}
    # The plaintext secret must never land in the audit trail.
    for r in rows:
        assert new_secret not in str(r.payload)


@pytest.mark.asyncio
async def test_public_event_catalog_matches_subscription_validation(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    """The public route must resolve before the UUID route and accept every choice."""
    from hailhq.core.webhook_events import WebhookEventType

    response = await client.get("/v1/webhooks/event-types")
    assert response.status_code == 200, response.text
    events = response.json()["items"]
    assert [event["type"] for event in events] == [
        event.value for event in WebhookEventType
    ]
    assert all(event["label"] and event["channel"] for event in events)
    assert {event["channel"] for event in events} == {"Email", "Calls", "SMS"}
    names = [event["type"] for event in events]

    created = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/hooks", "event_types": names},
        headers=auth_headers,
    )
    assert created.status_code == 201, created.text
    assert created.json()["event_types"] == names
    patched = await client.patch(
        f"/webhooks/{created.json()['id']}",
        json={"event_types": names[::-1]},
        headers=auth_headers,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["event_types"] == names[::-1]


@pytest.mark.asyncio
async def test_event_catalog_does_not_allow_unknown_subscription_events(
    client: httpx.AsyncClient, auth_headers: dict[str, str]
):
    response = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/hooks",
            "event_types": ["call.invented"],
        },
        headers=auth_headers,
    )
    assert response.status_code == 422
