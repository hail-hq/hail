"""Integration tests for the v1 emails API."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.hail_mail import org_prefix_from_id
from hailhq.core.models import ApiKey, AuditLog, Email, EmailDomain, UsageEvent

from .conftest import insert_org_and_key  # noqa: F401

# --------------------------------------------------------------------------- #
# Helpers — keep tests focused on the surface, not boilerplate.
# --------------------------------------------------------------------------- #


async def _register_custom_verified(
    client: httpx.AsyncClient,
    headers: dict,
    domain: str = "acme.com",
) -> str:
    """Register + verify a custom domain. Returns its email-domain id."""
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": domain},
        headers=headers,
    )
    assert created.status_code == 201
    await client.post(f"/email-domains/{created.json()['id']}/verify", headers=headers)
    return created.json()["id"]


async def _send_email(client: httpx.AsyncClient, plain: str) -> dict:
    """POST one outbound email. Caller must have a verified sender already."""
    resp = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "hello"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


# --------------------------------------------------------------------------- #
# POST /emails — validation
# --------------------------------------------------------------------------- #


async def test_post_emails_unauthenticated_returns_401(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "body"},
    )
    assert resp.status_code == 401


async def test_post_emails_rejects_invalid_recipient(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/emails",
        json={"to": ["not-an-email"], "subject": "hi", "body_text": "b"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


async def test_post_emails_requires_a_body(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/emails",
        json={"to": ["a@example.com"], "subject": "hi"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422
    assert "body_text or body_html" in resp.text


async def test_post_emails_requires_at_least_one_recipient(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/emails",
        json={"to": [], "subject": "hi", "body_text": "b"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 422


# --------------------------------------------------------------------------- #
# POST /emails — sender resolution
# --------------------------------------------------------------------------- #


async def test_post_emails_uses_verified_custom_domain_by_default(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")

    resp = await client.post(
        "/emails",
        json={"to": ["alice@example.com"], "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "sent"
    assert body["from_address"] == "noreply@acme.com"
    assert body["to_addresses"] == ["alice@example.com"]
    assert body["provider_message_id"]
    assert body["sent_at"] is not None

    email_mock.send_email.assert_awaited_once()
    call_kwargs = email_mock.send_email.call_args.kwargs
    assert call_kwargs["from_address"] == "noreply@acme.com"
    assert call_kwargs["to_addresses"] == ["alice@example.com"]


async def test_post_emails_auto_mints_hail_mail_when_no_sender_exists(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    org_id, _, plain = org_and_key
    resp = await client.post(
        "/emails",
        json={"to": ["alice@example.com"], "subject": "hi", "body_text": "body"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    expected_org = org_prefix_from_id(org_id)
    assert body["from_address"] == f"admin+{expected_org}@mail.hail.so"
    # An EmailDomain row should have been created on the fly.
    sd = (await async_session.execute(select(EmailDomain))).scalar_one()
    assert sd.kind == "hail_mail"
    assert sd.verification_status == "verified"
    assert sd.local_prefix_user == "admin"
    assert sd.local_prefix_org == expected_org


async def test_post_emails_auto_mint_recovers_from_race(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,
) -> None:
    """Two concurrent first-time POST /emails must not produce a 500.

    Simulates the race by pre-inserting the row a concurrent request would
    have created (same org, same composed address). The handler's auto-mint
    flush will collide on the (organization_id, domain) unique constraint;
    the recovery path should rollback and pick up the winning row.
    """
    from datetime import datetime, timezone

    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")
    org_id, _, plain = org_and_key
    expected_org = org_prefix_from_id(org_id)
    address = f"admin+{expected_org}@mail.hail.so"

    # Pre-seed the row that the concurrent request would have committed first.
    winning = EmailDomain(
        organization_id=org_id,
        kind="hail_mail",
        domain=address,
        local_prefix_user="admin",
        local_prefix_org=expected_org,
        verification_status="verified",
        dns_records=[],
        mail_from_domain=None,
        provider="ses",
        verified_at=datetime.now(timezone.utc),
    )
    async_session.add(winning)
    await async_session.commit()
    await async_session.refresh(winning)

    resp = await client.post(
        "/emails",
        json={"to": ["alice@example.com"], "subject": "hi", "body_text": "body"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    # Without the IntegrityError guard this is 500. With it, the email lands
    # against the winning row.
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_address"] == address

    # Exactly one EmailDomain row — recovery used the existing one, didn't
    # create a duplicate.
    rows = (await async_session.execute(select(EmailDomain))).scalars().all()
    assert len(rows) == 1
    assert rows[0].id == winning.id


async def test_post_emails_auto_mint_is_per_org_no_cross_org_conflict(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,
) -> None:
    """Two orgs sending their first email each auto-mint their OWN address.

    Previously the org prefix was a deploy-wide constant, so org A claimed
    the one address and org B's send 409'd on the global unique index. The
    org prefix is now derived per-org, so both sends succeed against distinct
    rows and no org can intercept another's mail.
    """
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "admin")

    org_a, _, plain_a = org_and_key
    org_b, _, plain_b = await insert_org_and_key(async_session)

    resp_a = await client.post(
        "/emails",
        json={"to": ["alice@example.com"], "subject": "hi", "body_text": "body"},
        headers={"Authorization": f"Bearer {plain_a}"},
    )
    resp_b = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "body"},
        headers={"Authorization": f"Bearer {plain_b}"},
    )
    assert resp_a.status_code == 201, resp_a.text
    assert resp_b.status_code == 201, resp_b.text

    from_a = resp_a.json()["from_address"]
    from_b = resp_b.json()["from_address"]
    assert from_a == f"admin+{org_prefix_from_id(org_a)}@mail.hail.so"
    assert from_b == f"admin+{org_prefix_from_id(org_b)}@mail.hail.so"
    assert from_a != from_b

    # Each org got its own row — neither blocked the other.
    rows = (await async_session.execute(select(EmailDomain))).scalars().all()
    assert {r.organization_id for r in rows} == {org_a, org_b}


async def test_post_emails_503_when_no_sender_and_no_hail_mail(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "hail_mail_base_domain", "")
    _, _, plain = org_and_key
    resp = await client.post(
        "/emails",
        json={"to": ["alice@example.com"], "subject": "hi", "body_text": "body"},
        headers={"Authorization": f"Bearer {plain}"},
    )
    assert resp.status_code == 503


async def test_post_emails_explicit_from_must_match_verified_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={
            "from": "evil@notmine.com",
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
        },
        headers=headers,
    )
    assert resp.status_code == 422
    assert "verified sender" in resp.text


async def test_post_emails_explicit_from_uses_local_part(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={
            "from": "alerts@acme.com",
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["from_address"] == "alerts@acme.com"
    assert email_mock.send_email.call_args.kwargs["from_address"] == "alerts@acme.com"


async def test_post_emails_explicit_from_is_case_insensitive_in_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    """Mixed-case ``from`` domain should still match a verified row.

    The verified row stores ``acme.com`` (lowercased at registration).
    Callers passing ``from="alerts@ACME.com"`` should not get a 422.
    """
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={
            "from": "alerts@ACME.com",
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    # Domain portion lowercased; local part preserved.
    assert resp.json()["from_address"] == "alerts@acme.com"


async def test_post_emails_to_is_case_insensitive_in_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    """Recipient domain is also normalized to lowercase before send."""
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={
            "to": ["Alice@EXAMPLE.COM"],
            "subject": "hi",
            "body_text": "body",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["to_addresses"] == ["Alice@example.com"]


async def test_post_emails_does_not_send_through_pending_domain(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pending custom row → 422 pointing at the verify endpoint.

    The earlier behaviour was a generic 503 ("set HAIL_MAIL_BASE_DOMAIN"),
    which misled operators who had registered a domain but not yet
    published its DKIM CNAMEs. The fix returns a specific 422 naming the
    pending domain so the operator knows exactly what to do.
    """
    # Make sure auto-mint doesn't fire as a fallback.
    monkeypatch.setattr(settings, "hail_mail_base_domain", "")
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "pending.com"},
        headers=headers,
    )
    assert created.status_code == 201
    # Do NOT verify — row stays pending.
    resp = await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "pending.com" in detail
    assert "verify" in detail


# --------------------------------------------------------------------------- #
# POST /emails — provider failure
# --------------------------------------------------------------------------- #


async def test_post_emails_marks_failed_when_provider_raises(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    email_mock.send_email.side_effect = RuntimeError("MessageRejected")

    resp = await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    assert resp.status_code == 502

    email = (await async_session.execute(select(Email))).scalar_one()
    assert email.status == "failed"
    assert email.end_reason == "RuntimeError"
    assert email.failed_at is not None

    actions = (
        (
            await async_session.execute(
                select(AuditLog.action)
                .where(AuditLog.resource_type == "email")
                .order_by(AuditLog.occurred_at.asc())
            )
        )
        .scalars()
        .all()
    )
    assert actions == ["email.create", "email.send_failed"]


# --------------------------------------------------------------------------- #
# POST /emails — idempotency
# --------------------------------------------------------------------------- #


async def test_post_emails_replays_with_same_idempotency_key(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {
        "Authorization": f"Bearer {plain}",
        "Idempotency-Key": "test-key-1",
    }
    await _register_custom_verified(client, headers, domain="acme.com")

    payload = {"to": ["x@example.com"], "subject": "hi", "body_text": "body"}
    r1 = await client.post("/emails", json=payload, headers=headers)
    assert r1.status_code == 201
    r2 = await client.post("/emails", json=payload, headers=headers)
    assert r2.status_code == 201
    assert r2.headers.get("idempotency-replay") == "true"
    assert r1.json()["id"] == r2.json()["id"]

    # Provider should have been called exactly once across the two requests.
    assert email_mock.send_email.await_count == 1

    rows = (await async_session.execute(select(Email))).scalars().all()
    assert len(rows) == 1


# --------------------------------------------------------------------------- #
# GET /emails/{id} and GET /emails
# --------------------------------------------------------------------------- #


async def test_get_emails_is_org_scoped(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    created = await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    email_id = created.json()["id"]

    # Same org reads its own row.
    me = await client.get(f"/emails/{email_id}", headers=headers)
    assert me.status_code == 200

    # Another org can't read it.
    _, _, other_plain = await insert_org_and_key(async_session)
    other = await client.get(
        f"/emails/{email_id}", headers={"Authorization": f"Bearer {other_plain}"}
    )
    assert other.status_code == 404


async def test_list_emails_filters_by_status(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")

    # One sent.
    await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "ok", "body_text": "b"},
        headers=headers,
    )
    # One failed.
    email_mock.send_email.side_effect = RuntimeError("nope")
    await client.post(
        "/emails",
        json={"to": ["y@example.com"], "subject": "bad", "body_text": "b"},
        headers=headers,
    )
    email_mock.send_email.side_effect = None  # reset for any later tests

    resp = await client.get("/emails?status=failed", headers=headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert items[0]["status"] == "failed"


async def test_list_emails_omits_message_bodies(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    """List response uses ``EmailSummary`` — bodies live on the detail row.

    Returning full ``body_text`` / ``body_html`` on every list page would
    leak PII into otherwise-cheap responses and balloon bandwidth on
    orgs with long mail histories. The detail endpoint still serves
    them.
    """
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    await client.post(
        "/emails",
        json={
            "to": ["x@example.com"],
            "subject": "hi",
            "body_text": "the body that should NOT appear in list",
            "body_html": "<p>also should NOT appear</p>",
        },
        headers=headers,
    )

    listed = await client.get("/emails", headers=headers)
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert "body_text" not in item
    assert "body_html" not in item
    # Spot-check that summary fields are still there.
    assert item["subject"] == "hi"
    assert item["status"] in {"queued", "sent"}

    # Detail endpoint still returns the bodies.
    detail = await client.get(f"/emails/{item['id']}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["body_text"] == "the body that should NOT appear in list"
    assert detail.json()["body_html"] == "<p>also should NOT appear</p>"


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


async def test_post_emails_round_trips_metadata(
    client: httpx.AsyncClient,
    org_and_key: tuple,
) -> None:
    """Caller-provided metadata is returned in the response."""
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={
            "to": ["x@example.com"],
            "subject": "hi",
            "body_text": "body",
            "metadata": {"campaign_id": "spring-2026", "tier": "free"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["metadata"] == {"campaign_id": "spring-2026", "tier": "free"}


async def test_post_emails_writes_audit_log(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "b"},
        headers=headers,
    )

    rows = (
        (
            await async_session.execute(
                select(AuditLog).where(AuditLog.action == "email.create")
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


_ = ApiKey  # type hint passthrough


# --------------------------------------------------------------------------- #
# Synthetic sent event
# --------------------------------------------------------------------------- #


async def test_post_emails_writes_synthetic_sent_event(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    """A successful send writes one EmailEvent(kind='sent') alongside the
    status flip, mirroring the CallEvent pattern for calls."""
    from hailhq.core.models import EmailEvent

    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={"to": ["bob@example.com"], "subject": "hi", "body_text": "hello"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    email_id = resp.json()["id"]

    events = (
        (
            await async_session.execute(
                select(EmailEvent).where(EmailEvent.email_id == UUID(email_id))
            )
        )
        .scalars()
        .all()
    )
    assert [e.kind for e in events] == ["sent"]
    assert events[0].occurred_at is not None
    assert events[0].payload == {}


# --------------------------------------------------------------------------- #
# Usage events
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "recipients,expected_units",
    [
        (["x@example.com"], 1),
        (["a@example.com", "b@example.com", "c@example.com"], 1),
    ],
)
async def test_post_emails_writes_usage_event(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
    recipients: list[str],
    expected_units: int,
) -> None:
    """Successful send writes one usage_events row; units = 1 (flat per send)."""
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={"to": recipients, "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    email_id = resp.json()["id"]

    row = (
        await async_session.execute(
            select(UsageEvent).where(UsageEvent.ref == f"email:{email_id}")
        )
    ).scalar_one()
    assert row.channel == "email"
    assert row.units == expected_units


async def test_post_emails_kicks_rater_after_usage_event(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A successful usage_events write fires the rater webhook.

    Without this kick the website's rater only sees the row on its next
    poll cycle, defeating the near-real-time billing contract.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        "hailhq.api.usage.notify_usage_event_recorded",
        lambda usage_event_id: calls.append(usage_event_id),
    )

    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")
    resp = await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert len(calls) == 1
    UUID(calls[0])  # well-formed usage_event id


async def test_outbound_email_meters_flat_one_unit(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    async_session: AsyncSession,
) -> None:
    """Outbound email meters exactly 1 unit regardless of recipient count.

    Sending to 2 To + 1 Cc + 1 Bcc (= 4 addresses) must produce a single
    usage_events row with units == 1, not 4.
    """
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")

    resp = await client.post(
        "/emails",
        json={
            "to": ["alice@example.com", "bob@example.com"],
            "cc": ["carol@example.com"],
            "bcc": ["dave@example.com"],
            "subject": "flat billing test",
            "body_text": "body",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    email_id = resp.json()["id"]

    row = (
        await async_session.execute(
            select(UsageEvent).where(UsageEvent.ref == f"email:{email_id}")
        )
    ).scalar_one()
    assert row.channel == "email"
    assert row.units == 1  # flat rate: 1¢ per send, not per recipient


async def test_post_emails_usage_event_failure_does_not_fail_send(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    monkeypatch: pytest.MonkeyPatch,
    async_session: AsyncSession,
) -> None:
    """A bookkeeping write failure must not break the user-facing send.

    Mirrors the audit-log pattern: usage_events runs in its own
    session_scope so the SES success and the response stay intact even
    if the bookkeeping write fails.
    """
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")

    class _BrokenSession:
        async def __aenter__(self):
            raise RuntimeError("db unreachable for bookkeeping")

        async def __aexit__(self, *_args):
            return False

    def _broken_session_scope():
        return _BrokenSession()

    monkeypatch.setattr(
        "hailhq.api.usage.session_scope",
        _broken_session_scope,
    )

    resp = await client.post(
        "/emails",
        json={"to": ["x@example.com"], "subject": "hi", "body_text": "body"},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "sent"

    email_row = (
        await async_session.execute(
            select(Email).where(Email.id == UUID(resp.json()["id"]))
        )
    ).scalar_one()
    assert email_row.status == "sent"


# --------------------------------------------------------------------------- #
# POST /emails — branding footer
# --------------------------------------------------------------------------- #


async def test_post_emails_appends_footer_on_wire_only(
    client: httpx.AsyncClient,
    org_and_key: tuple,
    email_mock: AsyncMock,
) -> None:
    """The provider send carries the branding footer; the stored row and
    API responses keep the tenant-authored body untouched."""
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    await _register_custom_verified(client, headers, domain="acme.com")

    resp = await client.post(
        "/emails",
        json={
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
            "body_html": "<p>body</p>",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text

    call_kwargs = email_mock.send_email.call_args.kwargs
    assert call_kwargs["body_text"].startswith("body")
    assert "Sent by Hail.so" in call_kwargs["body_text"]
    assert call_kwargs["body_text"].rstrip().endswith("(https://hail.so)")
    assert call_kwargs["body_html"].startswith("<p>body</p>")
    assert 'href="https://hail.so"' in call_kwargs["body_html"]

    # POST response and GET both return the original body, footer-free.
    assert resp.json()["body_text"] == "body"
    assert resp.json()["body_html"] == "<p>body</p>"
    got = await client.get(f"/emails/{resp.json()['id']}", headers=headers)
    assert got.status_code == 200
    assert got.json()["body_text"] == "body"
    assert got.json()["body_html"] == "<p>body</p>"
