"""Tests for the webhook delivery worker.

State-transition tests (_next_delivery_state) plus an integration tick
test against the real Postgres fixture and a stub http_post.
"""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core.models import EmailDomain, WebhookDelivery, WebhookSubscription
from hailhq.core.webhook_worker import (
    MAX_CONSECUTIVE_FAILURES,
    WebhookWorker,
    _next_delivery_state,
)
from hailhq.core.webhooks import RETRY_SCHEDULE_SECONDS
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.mark.asyncio
@pytest.mark.parametrize("receiver_raises", [False, True])
@pytest.mark.parametrize("spacing", [" ", "  "])
async def test_authenticated_receiver_gets_authorization_and_signed_event(
    async_session, receiver_raises, spacing
):
    from hailhq.core.secret_cipher import SecretCipher, generate_key

    cipher = SecretCipher(generate_key())
    authorization = "Bearer" + spacing + "cursor_receiver_test_key"
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted=cipher.encrypt("signing_secret"),
        authorization_encrypted=cipher.encrypt(authorization),
        event_types=["call.completed"],
    )
    async_session.add(sub)
    await async_session.commit()
    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="call.completed",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {}},
    )
    async_session.add(delivery)
    await async_session.commit()

    async def receiver(url, body, headers):
        assert headers.get("Authorization") == authorization
        assert headers["X-Hail-Signature"].startswith("t=")
        if receiver_raises:
            raise RuntimeError("x" * 4080 + authorization)
        return 200, "echo:" + authorization + '\n{"token":"cursor_receiver_test_key"}'

    worker = _build_worker(async_session, http_post=receiver, decrypt=cipher.decrypt)
    assert await worker.tick() == 1
    await async_session.refresh(delivery)
    assert delivery.status == ("pending" if receiver_raises else "succeeded")
    assert "cursor_receiver_test_key" not in delivery.response_body
    assert "cursor_" not in delivery.response_body


def _make_delivery(**kw) -> WebhookDelivery:
    return WebhookDelivery(
        id=uuid.uuid4(),
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload=kw.pop("payload", {"organization_id": str(uuid.uuid4()), "data": {}}),
        attempt=kw.pop("attempt", 0),
        status=kw.pop("status", "pending"),
        next_attempt_at=datetime.now(timezone.utc),
        **kw,
    )


def test_next_state_on_success_marks_succeeded():
    d = _make_delivery()
    new_status, next_at, attempt = _next_delivery_state(d, ok=True)
    assert new_status == "succeeded"
    assert next_at is None
    assert attempt == 1


def test_next_state_on_failure_schedules_retry():
    d = _make_delivery(attempt=0)
    new_status, next_at, attempt = _next_delivery_state(d, ok=False)
    assert new_status == "pending"
    assert attempt == 1
    assert next_at is not None
    assert next_at > datetime.now(timezone.utc) - timedelta(seconds=1)


def test_next_state_after_last_retry_is_dead():
    # The schedule has N slots; once we've used N retries, the next failure → dead.
    d = _make_delivery(attempt=len(RETRY_SCHEDULE_SECONDS))
    new_status, next_at, _ = _next_delivery_state(d, ok=False)
    assert new_status == "dead"
    assert next_at is None


def _build_worker(
    session: AsyncSession,
    *,
    http_post,
    decrypt=lambda c: c,
) -> WebhookWorker:
    @asynccontextmanager
    async def factory():
        # Reuse the test session — avoids a separate engine for testing.
        yield session

    return WebhookWorker(
        session_factory=factory,
        http_post=http_post,
        decrypt=decrypt,
        concurrency=4,
        poll_interval=0.01,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("url_userinfo", ["", "unused:password@"])
async def test_worker_reaches_authenticated_http_receiver(async_session, url_userinfo):
    from functools import partial
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread

    from hailhq.core.http_post import httpx_post
    from hailhq.core.secret_cipher import SecretCipher, generate_key
    from hailhq.core.webhooks import sign_payload

    authorization = "Bearer local_receiver_test_key"
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            if self.headers.get("Authorization") != authorization:
                self.send_response(401)
                self.end_headers()
                return
            signature = self.headers["X-Hail-Signature"]
            timestamp = int(signature.split(",")[0].split("=")[1])
            assert signature == sign_payload(
                body, "signing_secret", timestamp=timestamp
            )
            received.append(body)
            self.send_response(204)
            self.end_headers()

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target = f"http://{url_userinfo}127.0.0.1:{server.server_port}/events"
        post = partial(httpx_post, allow_private_networks=True)
        status, _ = await post(target, b"{}", {})
        assert status == 401
        cipher = SecretCipher(generate_key())
        org_id = uuid.uuid4()
        sub = WebhookSubscription(
            organization_id=org_id,
            target_url=target,
            secret_encrypted=cipher.encrypt("signing_secret"),
            authorization_encrypted=cipher.encrypt(authorization),
            event_types=["call.completed"],
        )
        async_session.add(sub)
        await async_session.commit()
        delivery = WebhookDelivery(
            subscription_id=sub.id,
            event_type="call.completed",
            event_id=uuid.uuid4(),
            payload={"organization_id": str(org_id), "data": {}},
        )
        async_session.add(delivery)
        await async_session.commit()
        assert (
            await _build_worker(
                async_session, http_post=post, decrypt=cipher.decrypt
            ).tick()
            == 1
        )
        await async_session.refresh(delivery)
        assert delivery.status == "succeeded"
        assert delivery.response_status == 204
        assert len(received) == 1
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
async def test_tick_delivers_pending_subscription_event(async_session):
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
    )
    async_session.add(delivery)
    await async_session.commit()

    captured: list[tuple[str, bytes, dict[str, str]]] = []

    async def fake_post(url, body, headers):
        captured.append((url, body, dict(headers)))
        return 204, ""

    worker = _build_worker(async_session, http_post=fake_post)
    processed = await worker.tick()
    assert processed == 1
    assert len(captured) == 1
    url, _, headers = captured[0]
    assert url == "https://hooks.example.com/h"
    assert headers["X-Hail-Event"] == "email.received"
    assert headers["X-Hail-Signature"].startswith("t=")
    assert headers["X-Hail-Subscription"] == str(sub.id)

    await async_session.refresh(delivery)
    assert delivery.status == "succeeded"

    await async_session.refresh(sub)
    assert sub.consecutive_failures == 0
    assert sub.last_success_at is not None


@pytest.mark.asyncio
async def test_tick_failure_schedules_retry(async_session):
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
    )
    async_session.add(delivery)
    await async_session.commit()

    async def fake_post(_url, _body, _headers):
        return 500, "server error"

    worker = _build_worker(async_session, http_post=fake_post)
    await worker.tick()

    await async_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt == 1
    assert delivery.next_attempt_at > datetime.now(timezone.utc)

    await async_session.refresh(sub)
    assert sub.consecutive_failures == 0  # only bumps on terminal 'dead'
    assert sub.last_failure_at is not None


@pytest.mark.asyncio
async def test_paused_delivery_preserves_failure_and_resumes_remaining_attempt(
    async_session,
):
    org_id = uuid.uuid4()
    failed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    due_at = failed_at + timedelta(minutes=1)
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["call.completed"],
        status="disabled",
        last_failure_at=failed_at,
    )
    async_session.add(sub)
    await async_session.commit()
    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="call.completed",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {}},
        attempt=6,
        response_status=401,
        response_body="receiver authentication required",
        next_attempt_at=due_at,
    )
    async_session.add(delivery)
    await async_session.commit()

    async def accept_delivery(_url, _body, _headers):
        return 204, ""

    worker = _build_worker(async_session, http_post=accept_delivery)
    assert await worker.tick() == 0

    await async_session.refresh(delivery)
    await async_session.refresh(sub)
    assert delivery.status == "pending"
    assert delivery.attempt == 6
    assert delivery.response_status == 401
    assert delivery.response_body == "receiver authentication required"
    assert delivery.next_attempt_at == due_at
    assert sub.last_failure_at == failed_at

    sub.status = "active"
    await async_session.commit()
    assert await worker.tick() == 1
    await async_session.refresh(delivery)
    assert delivery.status == "succeeded"
    assert delivery.attempt == 7
    assert delivery.response_status == 204


@pytest.mark.asyncio
async def test_disable_after_claim_preserves_last_http_failure(async_session):
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["call.completed"],
    )
    async_session.add(sub)
    await async_session.commit()
    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="call.completed",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {}},
        attempt=4,
        response_status=401,
        response_body="receiver authentication required",
    )
    async_session.add(delivery)
    await async_session.commit()

    async def unexpected_delivery(_url, _body, _headers):
        pytest.fail("A paused webhook must not send an HTTP request")

    worker = _build_worker(async_session, http_post=unexpected_delivery)
    assert await worker._claim_batch(async_session) == [delivery.id]
    sub.status = "disabled"
    await async_session.commit()
    await worker._sem.acquire()
    await worker._deliver(delivery.id)

    await async_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt == 4
    assert delivery.response_status == 401
    assert delivery.response_body == "receiver authentication required"


@pytest.mark.asyncio
async def test_tick_dead_after_last_retry_bumps_subscription(async_session):
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
        attempt=len(RETRY_SCHEDULE_SECONDS),  # one more failure → dead
    )
    async_session.add(delivery)
    await async_session.commit()

    async def fake_post(_url, _body, _headers):
        return 500, "server error"

    worker = _build_worker(async_session, http_post=fake_post)
    await worker.tick()

    await async_session.refresh(delivery)
    assert delivery.status == "dead"

    await async_session.refresh(sub)
    assert sub.consecutive_failures == 1


@pytest.mark.asyncio
async def test_tick_records_failure_when_decrypt_fails(async_session):
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="corrupt-ciphertext",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
    )
    async_session.add(delivery)
    await async_session.commit()

    posted = []

    async def fake_post(url, body, headers):
        posted.append(url)
        return 204, ""

    def bad_decrypt(_token: str) -> str:
        raise ValueError("invalid token")

    worker = _build_worker(async_session, http_post=fake_post, decrypt=bad_decrypt)
    await worker.tick()

    await async_session.refresh(delivery)
    assert delivery.status == "pending"  # not succeeded — no POST happened
    assert delivery.response_body == "secret decrypt failed"
    assert posted == []


@pytest.mark.asyncio
async def test_auto_disable_at_threshold(async_session):
    """A subscription with N-1 consecutive failures auto-disables on the next dead."""
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
        consecutive_failures=MAX_CONSECUTIVE_FAILURES - 1,
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
        attempt=len(RETRY_SCHEDULE_SECONDS),
    )
    async_session.add(delivery)
    await async_session.commit()

    async def fake_post(_url, _body, _headers):
        return 500, "boom"

    worker = _build_worker(async_session, http_post=fake_post)
    await worker.tick()

    await async_session.refresh(sub)
    assert sub.status == "disabled"
    assert sub.consecutive_failures == MAX_CONSECUTIVE_FAILURES


@pytest.mark.asyncio
async def test_delivered_body_has_full_envelope(async_session):
    """The bytes POSTed must carry all six top-level §5.2 keys."""
    import json

    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"email_id": "e1"}},
    )
    async_session.add(delivery)
    await async_session.commit()

    captured: dict = {}

    async def fake_post(url, body, headers):
        captured["body"] = body
        return 200, "ok"

    worker = _build_worker(async_session, http_post=fake_post)
    await worker.tick()

    assert "body" in captured, "fake_post was never called — delivery was not processed"
    env = json.loads(captured["body"])
    assert set(env) == {
        "id",
        "type",
        "api_version",
        "created_at",
        "organization_id",
        "data",
    }
    assert env["id"] == str(delivery.id)
    assert env["type"] == "email.received"
    assert env["organization_id"] == str(org_id)
    assert env["data"] == {"email_id": "e1"}


@pytest.mark.asyncio
async def test_malformed_payload_records_failure_not_zombie(async_session):
    """A delivery with a payload missing required keys must record a failure,
    not silently disappear into gather(..., return_exceptions=True)."""
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={},  # missing "organization_id" and "data"
    )
    async_session.add(delivery)
    await async_session.commit()

    posted = []

    async def fake_post(url, body, headers):
        posted.append(url)
        return 200, "ok"

    worker = _build_worker(async_session, http_post=fake_post)
    await worker.tick()

    # No POST should have been made — worker bailed out early.
    assert posted == []

    await async_session.refresh(delivery)
    # Status must have advanced (not stayed pending silently).
    assert delivery.response_body == "malformed payload"


@pytest.mark.asyncio
async def test_tick_picks_up_redelivered_dead_row(async_session):
    """A previously-dead row reset by the redeliver endpoint (pending, attempt=0,
    next_attempt_at in the past) must be picked up and delivered on the next tick."""
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
        status="pending",
        attempt=0,
        next_attempt_at=datetime.now(timezone.utc) - timedelta(minutes=5),
    )
    async_session.add(delivery)
    await async_session.commit()

    async def fake_post(_url, _body, _headers):
        return 204, ""

    worker = _build_worker(async_session, http_post=fake_post)
    processed = await worker.tick()
    assert processed == 1

    await async_session.refresh(delivery)
    assert delivery.status == "succeeded"
    assert delivery.attempt == 1
    assert delivery.succeeded_at is not None


@pytest.mark.asyncio
async def test_tick_emits_both_subscription_and_domain_headers(async_session):
    org_id = uuid.uuid4()
    dom = EmailDomain(
        organization_id=org_id,
        kind="custom",
        domain="example.com",
    )
    async_session.add(dom)
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()

    delivery = WebhookDelivery(
        subscription_id=sub.id,
        email_domain_id=dom.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
    )
    async_session.add(delivery)
    await async_session.commit()

    captured: list[dict[str, str]] = []

    async def fake_post(url, body, headers):
        captured.append(dict(headers))
        return 204, ""

    worker = _build_worker(async_session, http_post=fake_post)
    assert await worker.tick() == 1
    assert captured[0]["X-Hail-Subscription"] == str(sub.id)
    assert captured[0]["X-Hail-Email-Domain"] == str(dom.id)


@pytest.mark.asyncio
async def test_delivery_holds_no_session_while_posting(async_session, session_factory):
    """A slow customer server must not hold a pooled connection: the POST
    runs between two short sessions."""
    org_id = uuid.uuid4()
    sub = WebhookSubscription(
        organization_id=org_id,
        target_url="https://hooks.example.com/h",
        secret_encrypted="plain",
        event_types=["email.received"],
    )
    async_session.add(sub)
    await async_session.commit()
    delivery = WebhookDelivery(
        subscription_id=sub.id,
        event_type="email.received",
        event_id=uuid.uuid4(),
        payload={"organization_id": str(org_id), "data": {"id": "evt_x"}},
    )
    async_session.add(delivery)
    await async_session.commit()

    open_sessions = 0
    open_during_post: list[int] = []

    @asynccontextmanager
    async def factory():
        nonlocal open_sessions
        open_sessions += 1
        try:
            # Separate sessions, as in production: the row is detached
            # between the read and the result write.
            async with session_factory() as session:
                yield session
        finally:
            open_sessions -= 1

    async def fake_post(url, body, headers):
        open_during_post.append(open_sessions)
        return 500, "down"

    worker = WebhookWorker(
        session_factory=factory, http_post=fake_post, decrypt=lambda c: c
    )
    assert await worker.tick() == 1
    assert open_during_post == [0]
    await async_session.refresh(delivery)
    assert delivery.status == "pending"
    assert delivery.attempt == 1
    assert delivery.response_status == 500
