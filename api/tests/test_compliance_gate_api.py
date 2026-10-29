"""Integration tests for the pre-send compliance gate wired into
POST /calls, POST /emails, and GET/POST /unsubscribe.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.config import settings
from hailhq.core.models import ApiKey, AuditLog, Call, Email, Suppression, UsageEvent
from hailhq.core.unsubscribe import build_unsubscribe_url
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key  # noqa: F401


async def _register_custom_verified(
    client: httpx.AsyncClient,
    headers: dict,
    domain: str = "acme.com",
) -> str:
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": domain},
        headers=headers,
    )
    assert created.status_code == 201
    await client.post(f"/email-domains/{created.json()['id']}/verify", headers=headers)
    return created.json()["id"]


# --------------------------------------------------------------------------- #
# Suppression list — voice.
# --------------------------------------------------------------------------- #


async def test_post_calls_blocks_suppressed_recipient(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    add_phone_number,
) -> None:
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)
    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="+14155559999",
        channel="voice",
        reason="recipient_request",
        source="manual",
    )
    await async_session.commit()

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 403
    assert "suppression" in resp.json()["detail"]

    # No Call row was created for the blocked send.
    rows = (await async_session.execute(select(Call))).scalars().all()
    assert rows == []

    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "call.blocked")
        )
    ).scalar_one()
    assert audit.resource_id is None
    assert audit.payload["reason"] is not None
    assert audit.payload["checks"]["suppression_hit"] is True


# --------------------------------------------------------------------------- #
# Suppression list — email.
# --------------------------------------------------------------------------- #


async def test_post_emails_blocks_suppressed_recipient(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
) -> None:
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)

    await add_suppression(
        async_session,
        organization_id=org_id,
        recipient="bounced@example.com",
        channel="email",
        reason="bounced",
        source="bounce",
    )
    await async_session.commit()

    resp = await client.post(
        "/emails",
        json={
            "to": ["bounced@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert resp.status_code == 403
    assert "bounced@example.com" in resp.json()["detail"]

    rows = (await async_session.execute(select(Email))).scalars().all()
    assert rows == []

    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "email.blocked")
        )
    ).scalar_one()
    assert audit.resource_id is None
    assert audit.payload["checks"]["suppression_hit"] is True


async def test_post_emails_succeeds_carries_unsubscribe_headers(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    email_mock,
) -> None:
    """Happy-path send passes List-Unsubscribe headers to the provider and
    logs the compliance scrub result even though nothing was blocked."""
    _org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)

    resp = await client.post(
        "/emails",
        json={
            "to": ["bob@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    send_kwargs = email_mock.send_email.call_args.kwargs
    assert "List-Unsubscribe" in send_kwargs["headers"]
    assert send_kwargs["headers"]["List-Unsubscribe-Post"] == (
        "List-Unsubscribe=One-Click"
    )

    audit = (
        await async_session.execute(
            select(AuditLog).where(AuditLog.action == "email.create")
        )
    ).scalar_one()
    assert audit.payload["compliance"]["suppression_hit"] is False


@pytest.mark.parametrize(
    "recipients",
    [
        pytest.param({"to": ["bob@example.com", "dan@example.com"]}, id="two-to"),
        pytest.param(
            {"to": ["bob@example.com"], "cc": ["dan@example.com"]}, id="to-and-cc"
        ),
    ],
)
async def test_post_emails_multi_recipient_has_no_unsubscribe_headers(
    client: httpx.AsyncClient,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    email_mock,
    recipients: dict,
) -> None:
    """The one-click token names one address, but every recipient gets the
    same headers. With 2 or more recipients, send none, so a cc pressing
    the button cannot suppress the first `to` address."""
    _org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)

    resp = await client.post(
        "/emails",
        json={
            **recipients,
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    send_headers = email_mock.send_email.call_args.kwargs["headers"] or {}
    assert "List-Unsubscribe" not in send_headers
    assert "List-Unsubscribe-Post" not in send_headers


# --------------------------------------------------------------------------- #
# GET and POST /unsubscribe.
# --------------------------------------------------------------------------- #


async def test_unsubscribe_then_send_is_blocked(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_unsubscribe_secret", "test-secret")
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)

    url = build_unsubscribe_url("bob@example.com", org_id)
    token = url.split("token=", 1)[1]

    resp = await client.get(f"/unsubscribe?token={token}")
    assert resp.status_code == 200
    assert "bob@example.com" in resp.text

    row = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "bob@example.com")
        )
    ).scalar_one()
    assert row.channel == "email"
    assert row.source == "unsubscribe_link"
    assert row.organization_id == org_id

    send_resp = await client.post(
        "/emails",
        json={
            "to": ["bob@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert send_resp.status_code == 403


# GET is a person clicking the link; POST is the Gmail / Yahoo one-click
# button (RFC 8058 body, optional). Both mounts serve the route.
_UNSUBSCRIBE_CASES = [
    pytest.param("GET", "", None, id="get"),
    pytest.param("GET", "/v1", None, id="get-v1"),
    pytest.param("POST", "", "List-Unsubscribe=One-Click", id="post"),
    pytest.param("POST", "/v1", "List-Unsubscribe=One-Click", id="post-v1"),
    pytest.param("POST", "", None, id="post-no-body"),
]


def _unsubscribe_request_kwargs(body: str | None) -> dict:
    if body is None:
        return {}
    return {
        "content": body,
        "headers": {"Content-Type": "application/x-www-form-urlencoded"},
    }


@pytest.mark.parametrize(("method", "prefix", "body"), _UNSUBSCRIBE_CASES)
async def test_unsubscribe_valid_token_suppresses_recipient(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    prefix: str,
    body: str | None,
) -> None:
    monkeypatch.setattr(settings, "hail_unsubscribe_secret", "test-secret")
    org_id, _, _ = org_and_key
    url = build_unsubscribe_url("carol@example.com", org_id)
    token = url.split("token=", 1)[1]

    resp = await client.request(
        method,
        f"{prefix}/unsubscribe?token={token}",
        **_unsubscribe_request_kwargs(body),
    )
    assert resp.status_code == 200, resp.text
    assert "carol@example.com" in resp.text

    row = (
        await async_session.execute(
            select(Suppression).where(Suppression.recipient == "carol@example.com")
        )
    ).scalar_one()
    assert row.organization_id == org_id
    assert row.channel == "email"
    assert row.source == "unsubscribe_link"


@pytest.mark.parametrize(("method", "prefix", "body"), _UNSUBSCRIBE_CASES)
async def test_unsubscribe_rejects_invalid_token(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    method: str,
    prefix: str,
    body: str | None,
) -> None:
    resp = await client.request(
        method,
        f"{prefix}/unsubscribe?token=not-a-real-token",
        **_unsubscribe_request_kwargs(body),
    )
    assert resp.status_code == 400
    assert resp.headers["content-type"].startswith("text/html")
    rows = (await async_session.execute(select(Suppression))).scalars().all()
    assert rows == []


# --------------------------------------------------------------------------- #
# Premium-rate prefix block.
# --------------------------------------------------------------------------- #


async def test_post_calls_blocks_premium_rate_prefix(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    add_phone_number,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_blocked_e164_prefixes", "+1900")
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    resp = await client.post(
        "/calls",
        json={
            "to": "+19005551234",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 403
    assert "premium-rate" in resp.json()["detail"]


# --------------------------------------------------------------------------- #
# Velocity caps.
# --------------------------------------------------------------------------- #


async def test_post_calls_blocks_after_hourly_velocity_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    add_phone_number,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_velocity_call_per_hour", 2)
    org_id, _, plain = org_and_key
    await add_phone_number(async_session, org_id)

    for _ in range(2):
        async_session.add(
            UsageEvent(organization_id=org_id, channel="voice", units=1000)
        )
    await async_session.commit()

    resp = await client.post(
        "/calls",
        json={
            "to": "+14155559999",
            "system_prompt": "hi",
            "recipient_consent": True,
        },
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 403
    assert "velocity cap exceeded" in resp.json()["detail"]

    rows = (await async_session.execute(select(Call))).scalars().all()
    assert rows == []


async def test_post_emails_blocks_after_hourly_velocity_cap(
    client: httpx.AsyncClient,
    async_session: AsyncSession,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_velocity_email_per_hour", 1)
    org_id, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers)

    async_session.add(UsageEvent(organization_id=org_id, channel="email", units=1))
    await async_session.commit()

    resp = await client.post(
        "/emails",
        json={
            "to": ["bob@example.com"],
            "subject": "hi",
            "body_text": "hello",
            "recipient_consent": True,
        },
        headers=headers,
    )
    assert resp.status_code == 403
    assert "velocity cap exceeded" in resp.json()["detail"]
