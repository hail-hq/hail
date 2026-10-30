"""API behavior of the hail-mail rules: one address per org, one org per head."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from hailhq.api.routes.emails import resolve_sender
from hailhq.core.config import settings
from hailhq.core.hail_mail import ROLE_MAILBOXES
from hailhq.core.models import EmailDomain
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .conftest import insert_org_and_key


def _bearer(plain: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plain}"}


def _hail_mail(head: str, tail: str = "acme") -> dict[str, str]:
    return {"kind": "hail_mail", "local_prefix_user": head, "local_prefix_org": tail}


@pytest.fixture(autouse=True)
def _base_domain(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_from", "")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "")


@pytest.mark.parametrize("head", sorted(ROLE_MAILBOXES | {"noreply", "forwarder"}))
async def test_post_refuses_a_reserved_head(
    client: httpx.AsyncClient, org_and_key: tuple, head: str
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/email-domains", json=_hail_mail(head), headers=_bearer(plain)
    )
    assert resp.status_code == 422, resp.text
    assert f"user prefix {head!r} is reserved" in resp.text


async def test_patch_refuses_a_reserved_head(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _, _, plain = org_and_key
    created = await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain)
    )
    assert created.status_code == 201, created.text

    resp = await client.patch(
        f"/email-domains/{created.json()['id']}",
        json={"local_prefix_user": "postmaster"},
        headers=_bearer(plain),
    )
    assert resp.status_code == 422, resp.text
    assert "user prefix 'postmaster' is reserved" in resp.text


async def test_pinned_from_with_a_role_head_is_refused(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_from", "admin+selfhost@mail.hail.so")
    _, _, plain = org_and_key

    resp = await client.post(
        "/email-domains", json={"kind": "hail_mail"}, headers=_bearer(plain)
    )

    assert resp.status_code == 422, resp.text
    assert "user prefix 'admin' is reserved" in resp.text


async def test_role_name_as_the_default_prefix_still_mints(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-org token makes the head a different name."""
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    _, _, plain = org_and_key

    resp = await client.post(
        "/email-domains", json={"kind": "hail_mail"}, headers=_bearer(plain)
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["local_prefix_user"].startswith("admin-")


async def test_second_address_in_one_org_returns_409(
    client: httpx.AsyncClient, org_and_key: tuple
) -> None:
    _, _, plain = org_and_key
    first = await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain)
    )
    assert first.status_code == 201, first.text

    second = await client.post(
        "/email-domains", json=_hail_mail("bob"), headers=_bearer(plain)
    )

    assert second.status_code == 409, second.text
    assert second.json()["detail"] == (
        "this organization already has a hail-mail address; "
        "change it with PATCH /email-domains/{id}"
    )


async def test_head_retired_by_another_org_returns_409(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _, _, plain_a = org_and_key
    _, _, plain_b = await insert_org_and_key(async_session)
    created = await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain_a)
    )
    moved = await client.patch(
        f"/email-domains/{created.json()['id']}",
        json={"local_prefix_user": "bob"},
        headers=_bearer(plain_a),
    )
    assert moved.status_code == 200, moved.text

    resp = await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain_b)
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == (
        "hail-mail user prefix 'alice' is already taken; "
        "choose a different local_prefix_user"
    )


async def test_first_send_with_a_retired_pinned_head_returns_409(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    email_mock: AsyncMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, plain_a = org_and_key
    _, _, plain_b = await insert_org_and_key(async_session)
    created = await client.post(
        "/email-domains", json=_hail_mail("team", "shared"), headers=_bearer(plain_a)
    )
    await client.patch(
        f"/email-domains/{created.json()['id']}",
        json={"local_prefix_user": "other"},
        headers=_bearer(plain_a),
    )
    monkeypatch.setattr(settings, "hail_mail_from", "team+shared@mail.hail.so")

    resp = await client.post(
        "/emails",
        json={
            "to": ["x@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
        },
        headers=_bearer(plain_b),
    )

    assert resp.status_code == 409, resp.text
    assert "'team' is already taken" in resp.text
    email_mock.send_email.assert_not_awaited()


async def _verified_custom(session: AsyncSession, org_id, domain: str) -> None:
    session.add(
        EmailDomain(
            organization_id=org_id,
            kind="custom",
            domain=domain,
            verification_status="verified",
            verified_at=datetime.now(timezone.utc),
            provider="ses",
        )
    )
    await session.commit()


async def test_verified_custom_domain_wins_over_hail_mail(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    email_mock: AsyncMock,
) -> None:
    org_id, _, plain = org_and_key
    created = await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain)
    )
    assert created.status_code == 201, created.text
    await _verified_custom(async_session, org_id, "acme.com")

    resp = await client.post(
        "/emails",
        json={
            "to": ["x@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
        },
        headers=_bearer(plain),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_address"] == "noreply@acme.com"

    listed = await client.get("/email-domains", headers=_bearer(plain))
    assert listed.json()["default_from"] == "noreply@acme.com"


async def test_agent_pick_prefers_a_verified_custom_domain(
    async_session: AsyncSession,
) -> None:
    """The hail-mail row is older, so the old pick would have chosen it."""
    org_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    async_session.add(
        EmailDomain(
            organization_id=org_id,
            kind="hail_mail",
            domain="alice+acme@mail.hail.so",
            local_prefix_user="alice",
            local_prefix_org="acme",
            verification_status="verified",
            provider="ses",
            created_at=now - timedelta(days=1),
        )
    )
    await async_session.commit()
    await _verified_custom(async_session, org_id, "acme.com")

    sender = await resolve_sender(async_session, org_id, None, allow_ambiguous=True)

    assert sender.domain == "acme.com"


async def test_hail_mail_stays_available_through_an_explicit_from(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    email_mock: AsyncMock,
) -> None:
    org_id, _, plain = org_and_key
    await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain)
    )
    await _verified_custom(async_session, org_id, "acme.com")

    resp = await client.post(
        "/emails",
        json={
            "from": "alice+acme@mail.hail.so",
            "to": ["x@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
        },
        headers=_bearer(plain),
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_address"] == "alice+acme@mail.hail.so"


async def test_two_custom_domains_still_422_and_omit_hail_mail(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    email_mock: AsyncMock,
) -> None:
    org_id, _, plain = org_and_key
    await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain)
    )
    await _verified_custom(async_session, org_id, "acme.com")
    await _verified_custom(async_session, org_id, "acme.io")

    resp = await client.post(
        "/emails",
        json={
            "to": ["x@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
        },
        headers=_bearer(plain),
    )

    assert resp.status_code == 422, resp.text
    message = resp.json()["detail"][0]["msg"]
    assert "2 verified senders" in message
    assert "alice+acme" not in message


async def test_pending_custom_domain_does_not_replace_hail_mail(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    org_id, _, plain = org_and_key
    await client.post(
        "/email-domains", json=_hail_mail("alice"), headers=_bearer(plain)
    )
    async_session.add(
        EmailDomain(
            organization_id=org_id,
            kind="custom",
            domain="acme.com",
            verification_status="pending",
            provider="ses",
        )
    )
    await async_session.commit()

    listed = await client.get("/email-domains", headers=_bearer(plain))

    assert listed.json()["default_from"] == "alice+acme@mail.hail.so"
    rows = await async_session.execute(select(EmailDomain.kind))
    assert sorted(rows.scalars()) == ["custom", "hail_mail"]
