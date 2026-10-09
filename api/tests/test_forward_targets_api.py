"""Forward-target verification through the API: PATCH forward_to queues a
confirm mail for strangers only, /forward-targets lists + resends, and the
public confirm page needs a POST."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.forward_targets import MAX_PENDING_TARGETS, RESEND_COOLDOWN
from hailhq.core.models import Email, EmailDomain, EmailForwardTarget, User
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


@pytest.fixture()
async def org(async_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):
    from .conftest import insert_org_and_key

    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")

    org_id, api_key, plain = await insert_org_and_key(async_session)
    # The member row exists (insert_org_and_key); give it a verified login.
    async_session.add(
        User(
            id=uuid.UUID(api_key.reference_id),
            name="Alice",
            email="alice@acme.com",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    domain = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain="alice+acme@mail.hail.so",
        local_prefix_user="alice",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(domain)
    await async_session.commit()
    await async_session.refresh(domain)
    return org_id, {"Authorization": f"Bearer {plain}"}, domain


async def _queued_system(session: AsyncSession, org_id) -> list[Email]:
    rows = (
        await session.execute(
            select(Email)
            .where(Email.organization_id == org_id)
            .where(Email.metadata_["system_kind"].astext.isnot(None))
            .order_by(Email.created_at)
        )
    ).scalars()
    return list(rows)


def _token_from(body: str) -> str:
    marker = "confirm?token="
    start = body.index(marker) + len(marker)
    end = body.index("\n", start)
    return body[start:end]


@pytest.mark.asyncio
async def test_patch_queues_confirm_for_stranger_only(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    org_id, headers, domain = org
    r = await client.patch(
        f"/email-domains/{domain.id}",
        json={
            "inbound_enabled": True,
            "forward_to": ["Alice@acme.com", "ops@other.com"],
        },
        headers=headers,
    )
    assert r.status_code == 200, r.text

    r = await client.get("/forward-targets", headers=headers)
    assert r.status_code == 200
    items = {i["address"]: i for i in r.json()["items"]}
    assert items["alice@acme.com"]["status"] == "verified"
    assert items["ops@other.com"]["status"] == "pending"
    assert items["ops@other.com"]["token_sent_at"] is not None

    queued = await _queued_system(async_session, org_id)
    assert [q.to_addresses for q in queued] == [["ops@other.com"]]
    mail = queued[0]
    assert mail.from_address == "noreply+acme@mail.hail.so"
    assert mail.email_domain_id == domain.id
    assert mail.metadata_["system_kind"] == "forward_confirm"
    assert "/v1/forward-targets/confirm?token=" in mail.body_text
    assert mail.status == "queued"

    # Re-saving the same list sends nothing new.
    r = await client.patch(
        f"/email-domains/{domain.id}",
        json={"forward_to": ["ops@other.com", "alice@acme.com"]},
        headers=headers,
    )
    assert r.status_code == 200
    assert len(await _queued_system(async_session, org_id)) == 1


@pytest.mark.asyncio
async def test_patch_rejects_more_than_ten_targets(client: httpx.AsyncClient, org):
    _, headers, domain = org
    r = await client.patch(
        f"/email-domains/{domain.id}",
        json={"forward_to": [f"u{i}@example.com" for i in range(11)]},
        headers=headers,
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_confirm_page_requires_post_then_verifies(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    org_id, headers, domain = org
    await client.patch(
        f"/email-domains/{domain.id}",
        json={"forward_to": ["ops@other.com"]},
        headers=headers,
    )
    (mail,) = await _queued_system(async_session, org_id)
    token = _token_from(mail.body_text)

    # GET only renders the button — a link scanner must not confirm.
    r = await client.get("/v1/forward-targets/confirm", params={"token": token})
    assert r.status_code == 200
    assert "ops@other.com" in r.text and 'method="post"' in r.text
    r = await client.get("/forward-targets", headers=headers)
    assert r.json()["items"][0]["status"] == "pending"

    r = await client.post("/v1/forward-targets/confirm", data={"token": token})
    assert r.status_code == 200, r.text
    assert "Forwarding confirmed" in r.text
    r = await client.get("/forward-targets", headers=headers)
    item = r.json()["items"][0]
    assert item["status"] == "verified" and item["verified_at"] is not None
    assert item["token_sent_at"] is None

    # Token is single-use; a bad token is a 400 page.
    r = await client.post("/v1/forward-targets/confirm", data={"token": token})
    assert r.status_code == 400
    r = await client.get("/v1/forward-targets/confirm", params={"token": "garbage"})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_resend_cooldown_404_and_409(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    org_id, headers, domain = org
    await client.patch(
        f"/email-domains/{domain.id}",
        json={"forward_to": ["ops@other.com", "alice@acme.com"]},
        headers=headers,
    )

    r = await client.post("/forward-targets/ops@other.com/resend", headers=headers)
    assert r.status_code == 429
    assert "Retry-After" in r.headers

    target = (
        await async_session.execute(
            select(EmailForwardTarget).where(
                EmailForwardTarget.address == "ops@other.com"
            )
        )
    ).scalar_one()
    target.token_sent_at = (
        datetime.now(timezone.utc) - RESEND_COOLDOWN - timedelta(seconds=5)
    )
    await async_session.commit()

    r = await client.post("/forward-targets/OPS@other.com/resend", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "pending"
    assert len(await _queued_system(async_session, org_id)) == 2

    r = await client.post("/forward-targets/alice@acme.com/resend", headers=headers)
    assert r.status_code == 409
    r = await client.post("/forward-targets/nobody@other.com/resend", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_stopped_target_resend_restarts_after_confirm(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    org_id, headers, _domain = org
    async_session.add(
        EmailForwardTarget(
            organization_id=org_id,
            address="victim@other.com",
            status="stopped",
            stopped_reason="complaint",
            stopped_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()

    r = await client.get("/forward-targets", headers=headers)
    assert r.json()["items"][0]["stopped_reason"] == "complaint"

    r = await client.post("/forward-targets/victim@other.com/resend", headers=headers)
    assert r.status_code == 200, r.text
    (mail,) = await _queued_system(async_session, org_id)
    r = await client.post(
        "/v1/forward-targets/confirm", data={"token": _token_from(mail.body_text)}
    )
    assert r.status_code == 200
    r = await client.get("/forward-targets", headers=headers)
    item = r.json()["items"][0]
    assert item["status"] == "verified" and item["stopped_at"] is None


@pytest.mark.asyncio
async def test_forward_targets_are_org_scoped(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    _, headers, _ = org
    async_session.add(
        EmailForwardTarget(
            organization_id=uuid.uuid4(), address="other@org.com", status="verified"
        )
    )
    await async_session.commit()
    r = await client.get("/forward-targets", headers=headers)
    assert r.json()["items"] == []
    r = await client.post("/forward-targets/other@org.com/resend", headers=headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_caps_unconfirmed_addresses_across_saves(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    org_id, headers, domain = org
    r = await client.patch(
        f"/email-domains/{domain.id}",
        json={"forward_to": [f"s{i}@other.com" for i in range(MAX_PENDING_TARGETS)]},
        headers=headers,
    )
    assert r.status_code == 200, r.text
    r = await client.patch(
        f"/email-domains/{domain.id}",
        json={"forward_to": ["one-more@other.com"]},
        headers=headers,
    )
    assert r.status_code == 422, r.text
    assert "unconfirmed" in r.text
    assert len(await _queued_system(async_session, org_id)) == MAX_PENDING_TARGETS


@pytest.mark.asyncio
async def test_post_emails_rejects_reserved_metadata_keys(
    client: httpx.AsyncClient, org
):
    _, headers, _domain = org
    for key in ("system_kind", "forwarded_from", "forward_headers"):
        r = await client.post(
            "/emails",
            json={
                "to": ["bob@example.com"],
                "subject": "hi",
                "body_text": "hello",
                "recipient_consent": True,
                "metadata": {key: "x"},
            },
            headers=headers,
        )
        assert r.status_code == 422, (key, r.text)
        assert "reserved" in r.text


@pytest.mark.asyncio
async def test_same_list_saved_to_two_domain_rows_sends_one_confirm(
    client: httpx.AsyncClient, async_session: AsyncSession, org
):
    """The console writes one org-level list to every domain row. The second
    row must find the first row's target and send nothing new."""
    org_id, headers, domain = org
    custom = EmailDomain(
        organization_id=org_id,
        kind="custom",
        domain="inbox.acme.com",
        verification_status="verified",
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(custom)
    await async_session.commit()
    await async_session.refresh(custom)

    for did in (domain.id, custom.id):
        r = await client.patch(
            f"/email-domains/{did}",
            json={"inbound_enabled": True, "forward_to": ["stranger@other.com"]},
            headers=headers,
        )
        assert r.status_code == 200, r.text

    rows = (
        (
            await async_session.execute(
                select(EmailForwardTarget).where(
                    EmailForwardTarget.organization_id == org_id
                )
            )
        )
        .scalars()
        .all()
    )
    assert [(t.address, t.status) for t in rows] == [("stranger@other.com", "pending")]
    queued = await _queued_system(async_session, org_id)
    assert len(queued) == 1
    # Custom rows have no org prefix; the sender falls back to the id-derived one.
    assert queued[0].from_address == "noreply+acme@mail.hail.so"


@pytest.mark.asyncio
async def test_reserved_user_prefixes_cannot_be_minted_or_renamed(
    client: httpx.AsyncClient, org
):
    """noreply+<org>@ is the sender of every forward. Another org must not be
    able to own that address and collect the bounces."""
    _, headers, domain = org
    for prefix in ("noreply", "forwarder", " NoReply "):
        r = await client.post(
            "/email-domains",
            json={
                "kind": "hail_mail",
                "local_prefix_user": prefix,
                "local_prefix_org": "x",
            },
            headers=headers,
        )
        assert r.status_code == 422, (prefix, r.text)
        assert "reserved" in r.text
        r = await client.patch(
            f"/email-domains/{domain.id}",
            json={"local_prefix_user": prefix},
            headers=headers,
        )
        assert r.status_code == 422, (prefix, r.text)
