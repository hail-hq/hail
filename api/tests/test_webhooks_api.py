"""Tests for the org-wide webhook subscription CRUD surface."""

from __future__ import annotations

import asyncio
import json
import socket
from datetime import timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from hailhq.api.main import _request_span_arguments
from hailhq.api.routes import webhooks as webhooks_routes
from hailhq.core.config import settings
from hailhq.core.models import WebhookDelivery, WebhookSubscription
from hailhq.core.secret_cipher import SecretCipher, generate_key
from hailhq.core.webhooks import sign_payload
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("original_target", "target_change"),
    [
        ("https://hooks.example.com/receive", "https://other.example.com/hook"),
        ("https://hooks.example.com/receive", "https://hooks.example.com/receive/"),
        (
            "https://hooks.example.com/receive?value=one",
            "https://hooks.example.com/receive?value=one/",
        ),
    ],
)
async def test_receiver_authorization_is_encrypted_and_write_only(
    client,
    auth_headers,
    async_session,
    webhook_secret_key,
    original_target,
    target_change,
):
    authorization = "Bearer cursor_receiver_test_key"
    created = await client.post(
        "/webhooks",
        headers=auth_headers,
        json={
            "target_url": original_target,
            "event_types": ["call.completed"],
            "authorization": authorization,
        },
    )
    assert created.status_code == 201, created.text
    sub_id = created.json()["id"]
    assert created.json()["has_authorization"] is True
    assert authorization not in created.text
    sub = (
        await async_session.execute(
            select(WebhookSubscription).where(WebhookSubscription.id == sub_id)
        )
    ).scalar_one()
    assert sub.authorization_encrypted != authorization
    assert (
        SecretCipher(webhook_secret_key).decrypt(sub.authorization_encrypted)
        == authorization
    )
    for path in (f"/webhooks/{sub_id}", "/webhooks"):
        response = await client.get(path, headers=auth_headers)
        assert authorization not in response.text
        assert sub.authorization_encrypted not in response.text

    preserved = await client.patch(
        f"/webhooks/{sub_id}", headers=auth_headers, json={"status": "disabled"}
    )
    assert preserved.json()["has_authorization"] is True
    kept = await client.patch(
        f"/webhooks/{sub_id}", headers=auth_headers, json={"authorization": None}
    )
    assert kept.json()["has_authorization"] is True
    replacement = "Bearer replacement_receiver_key"
    replaced = await client.patch(
        f"/webhooks/{sub_id}", headers=auth_headers, json={"authorization": replacement}
    )
    assert replaced.status_code == 200, replaced.text
    assert replacement not in replaced.text
    await async_session.refresh(sub)
    assert (
        SecretCipher(webhook_secret_key).decrypt(sub.authorization_encrypted)
        == replacement
    )
    changed = await client.patch(
        f"/webhooks/{sub_id}",
        headers=auth_headers,
        json={"target_url": target_change},
    )
    assert changed.status_code == 422
    assert authorization not in changed.text
    assert replacement not in changed.text
    removed = await client.patch(
        f"/webhooks/{sub_id}",
        headers=auth_headers,
        json={"target_url": "https://other.example.com/hook", "authorization": ""},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["has_authorization"] is False
    assert removed.json()["status"] == "disabled"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "authorization",
    [
        "Bearer private_key\r\nX-Evil: yes",
        "Bearer private_key\0",
        "secret" * 400,
        "Bearer private_key\u2603",
        {"key": "private_key"},
        "   ",
        "Bearer   ",
    ],
    ids=[
        "newline",
        "null-byte",
        "oversized",
        "unicode",
        "invalid-type",
        "spaces",
        "scheme-only",
    ],
)
async def test_invalid_receiver_authorization_does_not_echo_credentials(
    client, auth_headers, authorization
):
    response = await client.post(
        "/webhooks",
        headers=auth_headers,
        json={
            "target_url": "https://hooks.example.com/receive",
            "event_types": ["call.completed"],
            "authorization": authorization,
        },
    )
    assert response.status_code == 422
    assert "private_key" not in response.text
    assert "secretsecret" not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["post", "patch"])
async def test_bare_scheme_authorization_is_rejected(client, auth_headers, method):
    body = {
        "target_url": "https://hooks.example.com/receive",
        "event_types": ["call.completed"],
    }
    path = "/webhooks"
    if method == "patch":
        created = await client.post("/webhooks", headers=auth_headers, json=body)
        assert created.status_code == 201, created.text
        path = f"/webhooks/{created.json()['id']}"
    response = await getattr(client, method)(
        path, headers=auth_headers, json={**body, "authorization": " basic "}
    )
    assert response.status_code == 422
    assert response.json()["detail"][0]["msg"] == (
        "Authorization header needs a credential after the scheme."
    )


@pytest.mark.parametrize(
    ("path", "kept"), [("/v1/webhooks", False), ("/v1/calls", True)]
)
def test_telemetry_drops_rejected_input_on_webhook_routes(path, kept):
    request = httpx.Request("POST", f"https://api.example.com{path}")
    error = {
        "type": "string_type",
        "loc": ["body", "authorization"],
        "msg": "Input should be a valid string",
        "input": {"key": "xk93hd72qq"},
    }
    attributes = {"values": {}, "errors": [error]}
    mapped = _request_span_arguments(request, attributes)
    assert ("input" in mapped["errors"][0]) is kept
    assert mapped["errors"][0]["loc"] == ["body", "authorization"]


@pytest.fixture(autouse=True)
def webhook_secret_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set a real Fernet key so encrypt/decrypt round-trips in the route."""
    key = generate_key()
    monkeypatch.setattr(settings, "hail_webhook_secret_key", key)
    return key


@pytest.fixture(autouse=True)
def example_hosts_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    """Resolve ``*.example.com`` to a public address, with no real DNS.

    The webhook guard refuses names that do not resolve, and most
    ``example.com`` subdomains have no DNS record. Other names (the test
    database host) use the real resolver.
    """
    real_getaddrinfo = socket.getaddrinfo

    def fake_getaddrinfo(host, port, *args, **kwargs):
        if host == "example.com" or (
            isinstance(host, str) and host.endswith(".example.com")
        ):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


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
    assert create.status_code == 201, create.text
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
    assert create.status_code == 201, create.text
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
    """A new subscription has updated_at == created_at. It catches one stamp
    going missing only when now() and the INSERT's time differ (by the
    auth statements or at least the BEGIN round trip); it cannot tell them
    apart when both are missing."""
    create = await client.post(
        "/webhooks",
        json={
            "target_url": "https://example.com/h",
            "event_types": ["call.summarized"],
        },
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
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
    """call.summarized sends only calls that ended after the subscription was
    created or last changed (hailhq.core.call_analysis), so every change
    bumps updated_at. (The test cannot tell now() from statement_timestamp():
    both fall inside the request.)"""
    create = await client.post(
        "/webhooks",
        json={"target_url": "https://example.com/h", "event_types": ["call.completed"]},
        headers=auth_headers,
    )
    assert create.status_code == 201, create.text
    sub_id = create.json()["id"]
    for change in (
        lambda: client.patch(
            f"/webhooks/{sub_id}",
            json={"event_types": ["call.completed", "call.summarized"]},
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
    assert create.status_code == 201, create.text
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
    assert create.status_code == 201, create.text
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
    assert create.status_code == 201, create.text
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
    assert create.status_code == 201, create.text
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
    assert row.payload == {
        "target_url": "https://hooks.example.com/audit",
        "has_authorization": False,
    }


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


class _FakeReceiver:
    """Stands in for httpx_post in the test-send route and records the call."""

    def __init__(
        self, status: int = 200, text: str = "ok", exc: Exception | None = None
    ):
        self.status, self.text, self.exc = status, text, exc
        self.delay = 0.0
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    async def __call__(self, url, body, headers, **kwargs):
        self.calls.append((url, body, headers))
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            if self.exc is None:
                raise
            raise self.exc from None  # a cancel that comes out as another error
        if self.exc is not None:
            raise self.exc
        return self.status, self.text


@pytest.fixture()
def receiver(monkeypatch: pytest.MonkeyPatch) -> _FakeReceiver:
    fake = _FakeReceiver()
    monkeypatch.setattr(webhooks_routes, "httpx_post", fake)
    return fake


@pytest.mark.asyncio
async def test_test_send_for_new_endpoint_is_unsigned_and_redacts_echo(
    client, auth_headers, receiver
):
    receiver.text = "seen Bearer receiver_key_123 here"
    response = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={
            "target_url": "https://hooks.example.com/receive",
            "authorization": "Bearer receiver_key_123",
        },
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["ok"] is True
    assert result["status_code"] == 200
    assert result["signed"] is False
    assert "receiver_key_123" not in result["response_body"]
    url, body, headers = receiver.calls[0]
    assert url == "https://hooks.example.com/receive"
    assert headers["Authorization"] == "Bearer receiver_key_123"
    assert headers["X-Hail-Event"] == "webhook.test"
    assert "X-Hail-Signature" not in headers
    assert json.loads(body)["type"] == "webhook.test"


@pytest.mark.asyncio
async def test_test_send_for_saved_subscription_signs_and_uses_saved_header(
    client, auth_headers, receiver
):
    created = await client.post(
        "/webhooks",
        headers=auth_headers,
        json={
            "target_url": "https://hooks.example.com/receive",
            "event_types": ["call.completed"],
            "authorization": "Bearer saved_receiver_key",
        },
    )
    assert created.status_code == 201, created.text
    secret, sub_id = created.json()["secret"], created.json()["id"]

    response = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={
            "target_url": "https://hooks.example.com/receive",
            "subscription_id": sub_id,
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["signed"] is True
    _, body, headers = receiver.calls[0]
    assert headers["Authorization"] == "Bearer saved_receiver_key"
    assert headers["X-Hail-Subscription"] == sub_id
    timestamp = int(headers["X-Hail-Signature"].split(",")[0].removeprefix("t="))
    assert headers["X-Hail-Signature"] == sign_payload(
        body, secret, timestamp=timestamp
    )

    removed = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={
            "target_url": "https://hooks.example.com/receive",
            "subscription_id": sub_id,
            "authorization": "",
        },
    )
    assert removed.status_code == 200, removed.text
    assert "Authorization" not in receiver.calls[1][2]

    moved = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={
            "target_url": "https://other.example.com/hook",
            "subscription_id": sub_id,
        },
    )
    assert moved.status_code == 422
    assert len(receiver.calls) == 2


@pytest.mark.asyncio
async def test_test_send_reports_receiver_errors(client, auth_headers, receiver):
    receiver.status, receiver.text = 401, "bad key"
    rejected = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={"target_url": "https://hooks.example.com/receive"},
    )
    assert rejected.json()["ok"] is False
    assert rejected.json()["status_code"] == 401
    assert rejected.json()["response_body"] == "bad key"

    receiver.exc = httpx.ReadTimeout("timed out")
    timed_out = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={"target_url": "https://hooks.example.com/receive"},
    )
    assert timed_out.status_code == 200
    assert timed_out.json()["ok"] is False
    assert timed_out.json()["status_code"] is None
    assert timed_out.json()["error"] == "No answer from your endpoint in time."

    receiver.exc = httpx.ConnectTimeout("timed out")
    no_connect = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={"target_url": "https://hooks.example.com/receive"},
    )
    assert no_connect.json()["error"] == "Could not connect to your endpoint in time."

    # A TimeoutError from the sender itself is not the route's deadline.
    receiver.exc = TimeoutError()
    inner = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={"target_url": "https://hooks.example.com/receive"},
    )
    assert inner.json()["error"] == "No answer from your endpoint in time."


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_as", [None, httpx.ReadError("closed")])
async def test_test_send_deadline_names_the_time(
    client, auth_headers, receiver, monkeypatch, cancel_as
):
    # The route's own deadline ends a slow endpoint and says so, also when
    # the cancel comes out of the sender as another error.
    monkeypatch.setattr(webhooks_routes, "_TEST_TIMEOUT_SECONDS", 0.05)
    receiver.delay = 30.0
    receiver.exc = cancel_as
    slow = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={"target_url": "https://hooks.example.com/receive"},
    )
    assert slow.json()["error"] == "The test took longer than 0.05 seconds."
    # The deadline ends the send: the 30 second receiver is not awaited.
    assert 50 <= slow.json()["duration_ms"] < 2000


@pytest.mark.asyncio
async def test_test_send_refuses_bad_targets_and_other_orgs(
    client, auth_headers, async_session, receiver
):
    from .conftest import insert_org_and_key

    http = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={"target_url": "http://hooks.example.com/receive"},
    )
    assert http.status_code == 422

    _, _, other_key = await insert_org_and_key(async_session)
    other = await client.post(
        "/webhooks",
        headers={"Authorization": f"Bearer {other_key}"},
        json={
            "target_url": "https://hooks.example.com/receive",
            "event_types": ["call.completed"],
        },
    )
    assert other.status_code == 201, other.text
    foreign = await client.post(
        "/webhooks/test",
        headers=auth_headers,
        json={
            "target_url": "https://hooks.example.com/receive",
            "subscription_id": other.json()["id"],
        },
    )
    assert foreign.status_code == 404
    assert receiver.calls == []


@pytest.mark.asyncio
async def test_test_send_holds_no_db_transaction_during_post(
    client, auth_headers, async_session, monkeypatch
):
    seen: list[bool] = []

    async def fake_post(url, body, headers, **kwargs):
        seen.append(async_session.in_transaction())
        return 200, "ok"

    monkeypatch.setattr(webhooks_routes, "httpx_post", fake_post)
    # The second request skips the throttled API key stamp, which commits.
    for _ in range(2):
        response = await client.post(
            "/webhooks/test",
            headers=auth_headers,
            json={"target_url": "https://hooks.example.com/receive"},
        )
        assert response.status_code == 200, response.text
    assert seen == [False, False]


@pytest.mark.asyncio
async def test_test_send_has_its_own_per_minute_limit(client, auth_headers, receiver):
    body = {"target_url": "https://hooks.example.com/receive"}
    for _ in range(webhooks_routes._TEST_LIMIT_PER_MINUTE):
        ok = await client.post("/webhooks/test", headers=auth_headers, json=body)
        assert ok.status_code == 200, ok.text
    limited = await client.post("/webhooks/test", headers=auth_headers, json=body)
    assert limited.status_code == 429
    assert "Retry-After" in limited.headers
    # The headers name the test bucket, not the general one.
    assert limited.headers["RateLimit-Limit"] == str(
        webhooks_routes._TEST_LIMIT_PER_MINUTE
    )
    assert limited.headers["RateLimit-Remaining"] == "0"
