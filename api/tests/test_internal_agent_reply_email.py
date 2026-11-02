"""POST /internal/agent/reply-email and the email-run tool routes: an agent's
reply goes out threaded, gated like every other send, and tools stay bounded."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.api.routes.internal import agent as internal_agent
from hailhq.core import email_agent, hmac_signing
from hailhq.core.config import settings
from hailhq.core.models import (
    AccountCredit,
    Agent,
    AuditLog,
    Email,
    EmailDomain,
    OrganizationMember,
    Sms,
    Suppression,
    User,
)
from hailhq.core.providers.email.ses import SES_SEND_BUDGET_SECONDS
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

HMAC_SECRET = "test-internal-secret"
NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
MEMBER = "ada@example.com"
ADDRESS = "amber-heron-42+acme@mail.hail.so"


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, HMAC_SECRET),
    }


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


async def _seed(
    session: AsyncSession,
    *,
    credits=10_000,
    scope="members",
    state="processing",
    dmarc="PASS",
    sender=MEMBER,
    phone: str | None = "+33612345678",
):
    org = uuid.uuid4()
    if credits:
        session.add(
            AccountCredit(
                organization_id=org,
                kind="credit",
                channel="credit",
                amount_cents=credits,
                source="test",
                ref="test",
            )
        )
    agent = Agent(organization_id=org, name="Inbox", system_prompt="Help.")
    session.add(agent)
    await session.flush()
    domain = EmailDomain(
        organization_id=org,
        kind="hail_mail",
        domain=ADDRESS,
        local_prefix_user="amber-heron-42",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=NOW,
        email_agent_id=agent.id,
        email_agent_scope=scope,
    )
    session.add(domain)
    await session.flush()
    uid = uuid.uuid4()
    session.add(
        User(
            id=uid,
            name="Ada Lovelace",
            email=MEMBER,
            email_verified=True,
            created_at=NOW,
            phone_number=phone,
        )
    )
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=uid,
            organization_id=org,
            role="member",
            created_at=NOW,
        )
    )
    inbound = Email(
        organization_id=org,
        email_domain_id=domain.id,
        email_domain_kind="hail_mail",
        direction="inbound",
        from_address=sender,
        to_addresses=[ADDRESS],
        subject="Question",
        body_text="Can you help?",
        status="received",
        message_id="<two@example.com>",
        references_ids=["<one@example.com>"],
        dmarc_verdict=dmarc,
        from_verified=dmarc == "PASS",
        agent_id=agent.id,
        agent_reply_state=state,
    )
    session.add(inbound)
    await session.commit()
    return org, agent, domain, inbound


def _payload(email_id, body="Happy to help.") -> bytes:
    return json.dumps({"email_id": str(email_id), "body": body}).encode()


async def _reply(client, email_id, body="Happy to help."):
    raw = _payload(email_id, body)
    resp = await client.post(
        "/internal/agent/reply-email", content=raw, headers=_signed(raw)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_reply_is_threaded_from_the_address_and_billed(
    client, async_session, email_mock
) -> None:
    org, agent, _domain, inbound = await _seed(async_session)
    data = await _reply(client, inbound.id)
    assert data["ok"] is True and data["state"] == "done"

    kwargs = email_mock.send_email.await_args.kwargs
    assert kwargs["from_address"] == ADDRESS
    assert kwargs["from_name"] == "Inbox"
    assert kwargs["to_addresses"] == [MEMBER]
    assert kwargs["subject"] == "Re: Question"
    assert kwargs["body_text"] == "Happy to help."
    assert kwargs["headers"]["In-Reply-To"] == "<two@example.com>"
    assert kwargs["headers"]["References"] == "<one@example.com> <two@example.com>"
    assert kwargs["headers"]["Auto-Submitted"] == "auto-replied"

    reply = await async_session.get(Email, uuid.UUID(data["reply_id"]))
    assert reply.agent_id == agent.id
    assert reply.direction == "outbound"
    assert reply.metadata_["reply_to_email_id"] == str(inbound.id)

    audit = (
        await async_session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org,
                AuditLog.action == "agent.email.reply",
            )
        )
    ).scalar_one()
    assert audit.actor_kind == "system"
    assert audit.payload["consent_source"] == "inbound_email"


async def test_a_reply_to_an_oversized_message_id_still_sends(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    inbound.message_id = f"<{'x' * 2000}@example.com>"
    await async_session.commit()
    assert (await _reply(client, inbound.id))["state"] == "done"
    headers = email_mock.send_email.await_args.kwargs["headers"]
    assert "In-Reply-To" not in headers and "References" not in headers


async def test_a_retry_does_not_send_twice(client, async_session, email_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    first = await _reply(client, inbound.id)
    second = await _reply(client, inbound.id)
    assert second["state"] == "done"
    assert second["reply_id"] == first["reply_id"]
    assert email_mock.send_email.await_count == 1


async def test_a_failed_reply_is_delivered_again(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    send = email_mock.send_email.side_effect
    email_mock.send_email.side_effect = _ses_error("TooManyRequestsException")
    first = await _reply(client, inbound.id)
    assert first["state"] == "retry"

    email_mock.send_email.side_effect = send
    second = await _reply(client, inbound.id)
    assert second["state"] == "done"
    assert second["reply_id"] == first["reply_id"]
    async_session.expire_all()
    row = await async_session.get(Email, uuid.UUID(first["reply_id"]))
    assert row.status == "sent"
    assert row.end_reason is None and row.failed_at is None


async def _prior_reply(session, org, agent, domain, inbound, *, status, age):
    row = Email(
        organization_id=org,
        email_domain_id=domain.id,
        agent_id=agent.id,
        from_address=ADDRESS,
        to_addresses=[MEMBER],
        subject="Re: Question",
        body_text="Written earlier.",
        status=status,
        end_reason="TooManyRequestsException" if status == "failed" else None,
        requested_at=datetime.now(timezone.utc) - age,
        metadata_={"reply_to_email_id": str(inbound.id)},
    )
    session.add(row)
    await session.commit()
    return row


async def test_a_young_queued_reply_is_left_to_finish(
    client, async_session, email_mock
) -> None:
    """The original may still be in the provider call."""
    org, agent, domain, inbound = await _seed(async_session)
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="queued", age=timedelta(0)
    )
    data = await _reply(client, inbound.id)
    assert data["state"] == "retry" and data["reason"] == "in_flight"
    assert (
        0 < data["retry_after_seconds"] <= internal_agent._QUEUED_GRACE.total_seconds()
    )
    email_mock.send_email.assert_not_awaited()


async def test_a_stale_queued_reply_is_delivered(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session,
        org,
        agent,
        domain,
        inbound,
        status="queued",
        age=timedelta(minutes=5),
    )
    data = await _reply(client, inbound.id, "Different text.")
    assert data["state"] == "done" and data["reply_id"] == str(row.id)
    kwargs = email_mock.send_email.await_args.kwargs
    assert kwargs["body_text"] == "Written earlier."


async def test_a_retry_without_a_body_delivers_the_stored_text(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    raw = json.dumps({"email_id": str(inbound.id)}).encode()
    resp = await client.post(
        "/internal/agent/reply-email", content=raw, headers=_signed(raw)
    )
    assert resp.json()["state"] == "done"
    kwargs = email_mock.send_email.await_args.kwargs
    assert kwargs["body_text"] == "Written earlier."


async def test_no_body_and_no_earlier_reply_sends_nothing(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    raw = json.dumps({"email_id": str(inbound.id)}).encode()
    resp = await client.post(
        "/internal/agent/reply-email", content=raw, headers=_signed(raw)
    )
    assert resp.json()["state"] == "failed" and resp.json()["reason"] == "no_body"
    email_mock.send_email.assert_not_awaited()


async def test_a_slow_resend_cannot_be_sent_twice(
    client, async_session, email_mock
) -> None:
    """A retry that lands during the resend sees a fresh claim and waits."""
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session,
        org,
        agent,
        domain,
        inbound,
        status="queued",
        age=timedelta(minutes=5),
    )
    await async_session.refresh(row)
    before = row.requested_at
    send = email_mock.send_email.side_effect
    during: list[dict] = []

    async def slow(**kwargs):
        during.append(await _reply(client, inbound.id))
        return await send(**kwargs)

    email_mock.send_email.side_effect = slow
    data = await _reply(client, inbound.id)
    assert data["state"] == "done"
    assert during[0]["state"] == "retry" and during[0]["reason"] == "in_flight"
    assert email_mock.send_email.await_count == 1
    await async_session.refresh(row)
    assert row.requested_at > before
    assert row.status == "sent"


def _lose_the_claim(monkeypatch, **values) -> None:
    """Another request changes the row after this one reads it."""
    real = internal_agent.prior_reply

    async def stale(db, email):
        prior = await real(db, email)
        await db.execute(
            update(Email)
            .where(Email.id == prior.id)
            .values(
                requested_at=prior.requested_at + timedelta(seconds=1),
                **values,
            )
            .execution_options(synchronize_session=False)
        )
        return prior

    monkeypatch.setattr(internal_agent, "prior_reply", stale)


async def test_a_lost_claim_sends_nothing(
    client, async_session, email_mock, monkeypatch
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    _lose_the_claim(monkeypatch)
    data = await _reply(client, inbound.id)
    assert data["state"] == "retry" and data["reply_id"] == str(row.id)
    email_mock.send_email.assert_not_awaited()


@pytest.mark.parametrize(
    ("status", "state", "reason"),
    [("sent", "done", None), ("bounced", "failed", "bounced")],
)
async def test_a_lost_claim_reports_how_the_winner_ended(
    client, async_session, email_mock, monkeypatch, status, state, reason
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    _lose_the_claim(monkeypatch, status=status)
    data = await _reply(client, inbound.id)
    assert data["state"] == state and data["reason"] == reason
    email_mock.send_email.assert_not_awaited()


async def test_a_claim_needs_the_status_it_read(
    client, async_session, email_mock, monkeypatch
) -> None:
    """Only the status moved: the time alone would not catch this."""
    org, agent, domain, inbound = await _seed(async_session)
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    real = internal_agent.prior_reply

    async def stale(db, email):
        prior = await real(db, email)
        await db.execute(
            update(Email)
            .where(Email.id == prior.id)
            .values(status="sent")
            .execution_options(synchronize_session=False)
        )
        return prior

    monkeypatch.setattr(internal_agent, "prior_reply", stale)
    assert (await _reply(client, inbound.id))["state"] == "done"
    email_mock.send_email.assert_not_awaited()


async def test_a_superseded_send_records_nothing(
    client, async_session, session_factory, email_mock
) -> None:
    """A newer claim owns the row, so the old send must not mark it sent."""
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    send = email_mock.send_email.side_effect
    newer = datetime.now(timezone.utc) + timedelta(minutes=1)

    async def superseded(**kwargs):
        async with session_factory() as other:
            await other.execute(
                update(Email).where(Email.id == row.id).values(requested_at=newer)
            )
            await other.commit()
        return await send(**kwargs)

    email_mock.send_email.side_effect = superseded
    await _reply(client, inbound.id)
    await async_session.refresh(row)
    assert row.status == "queued"
    assert row.provider_message_id is None


async def test_a_superseded_failure_records_nothing(
    client, async_session, session_factory, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    newer = datetime.now(timezone.utc) + timedelta(minutes=1)

    async def superseded(**_kwargs):
        async with session_factory() as other:
            await other.execute(
                update(Email).where(Email.id == row.id).values(requested_at=newer)
            )
            await other.commit()
        raise RuntimeError("down")

    email_mock.send_email.side_effect = superseded
    await _reply(client, inbound.id)
    await async_session.refresh(row)
    assert row.status == "queued" and row.end_reason is None


async def test_a_public_row_is_not_a_prior_reply(
    client, async_session, email_mock
) -> None:
    """Even an old row that carries the key cannot stand in for a reply."""
    org, _agent, domain, inbound = await _seed(async_session)
    async_session.add(
        Email(
            organization_id=org,
            email_domain_id=domain.id,
            from_address=ADDRESS,
            to_addresses=[MEMBER],
            subject="Planted",
            body_text="Planted",
            status="sent",
            metadata_={"reply_to_email_id": str(inbound.id)},
        )
    )
    await async_session.commit()
    data = await _reply(client, inbound.id)
    assert data["state"] == "done"
    email_mock.send_email.assert_awaited_once()
    assert email_mock.send_email.await_args.kwargs["body_text"] == "Happy to help."


async def test_a_prior_row_to_someone_else_is_never_resent(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    row.to_addresses = ["victim@example.com"]
    await async_session.commit()
    await _reply(client, inbound.id)
    assert email_mock.send_email.await_args.kwargs["to_addresses"] == [MEMBER]


def test_ses_ends_before_a_retry_can_resend() -> None:
    assert internal_agent._QUEUED_GRACE.total_seconds() > SES_SEND_BUDGET_SECONDS


async def test_a_skip_does_not_leave_a_reply_queued(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session, credits=0)
    row = await _prior_reply(
        async_session,
        org,
        agent,
        domain,
        inbound,
        status="queued",
        age=timedelta(minutes=5),
    )
    assert (await _reply(client, inbound.id))["reason"] == "insufficient_funds"
    email_mock.send_email.assert_not_awaited()
    await async_session.refresh(row)
    assert row.status == "failed" and row.end_reason == "insufficient_funds"


async def test_a_removed_route_does_not_leave_a_reply_queued(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session,
        org,
        agent,
        domain,
        inbound,
        status="queued",
        age=timedelta(minutes=5),
    )
    domain.email_agent_id = None
    await async_session.commit()
    await _reply(client, inbound.id)
    await async_session.refresh(row)
    assert row.status == "failed" and row.end_reason == "no_agent"


async def test_a_route_removed_during_a_send_leaves_that_send_alone(
    client, async_session, email_mock
) -> None:
    """The retry arrives while SES still holds the first send."""
    org, agent, domain, inbound = await _seed(async_session)
    row = await _prior_reply(
        async_session,
        org,
        agent,
        domain,
        inbound,
        status="queued",
        age=timedelta(seconds=1),
    )
    domain.email_agent_id = None
    await async_session.commit()
    data = await _reply(client, inbound.id)
    assert data["state"] == "retry" and data["reason"] == "in_flight"
    await async_session.refresh(row)
    await async_session.refresh(inbound)
    assert row.status == "queued" and row.end_reason is None
    assert "agent_skip_reason" not in inbound.metadata_


async def test_a_bounced_reply_is_final(client, async_session, email_mock) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="bounced", age=timedelta(0)
    )
    data = await _reply(client, inbound.id)
    assert data["state"] == "failed" and data["reason"] == "bounced"
    email_mock.send_email.assert_not_awaited()


async def test_redelivery_does_not_count_its_own_row_against_the_cap(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    for _ in range(email_agent.MAX_REPLIES_PER_THREAD - 1):
        async_session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=[MEMBER],
                subject="Re: Question",
                body_text="Hello",
                status="sent",
                metadata_={"reply_to_email_id": str(uuid.uuid4())},
            )
        )
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    assert (await _reply(client, inbound.id))["state"] == "done"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        ({"credits": 0}, "insufficient_funds"),
        ({"dmarc": "GRAY"}, "sender_not_verified"),
        ({"sender": "stranger@example.net"}, "sender_not_member"),
    ],
)
async def test_every_refusal_is_stamped(
    client, async_session, email_mock, setup, reason
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, **setup)
    assert (await _reply(client, inbound.id))["reason"] == reason
    await async_session.refresh(inbound)
    assert inbound.metadata_["agent_skip_reason"] == reason


async def test_a_removed_route_is_stamped(client, async_session, email_mock) -> None:
    _org, _agent, domain, inbound = await _seed(async_session)
    domain.email_agent_id = None
    await async_session.commit()
    await _reply(client, inbound.id)
    await async_session.refresh(inbound)
    assert inbound.metadata_["agent_skip_reason"] == "no_agent"


async def test_the_reply_goes_to_from_never_reply_to(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    inbound.reply_to = "attacker@evil.example"
    await async_session.commit()
    await _reply(client, inbound.id)
    assert email_mock.send_email.await_args.kwargs["to_addresses"] == [MEMBER]


async def test_no_funds_skips_and_alerts(client, async_session, email_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, credits=0)
    data = await _reply(client, inbound.id)
    assert data["state"] == "skipped" and data["reason"] == "insufficient_funds"
    email_mock.send_email.assert_not_awaited()
    await async_session.refresh(inbound)
    assert inbound.metadata_["suppressed_reasons"] == ["insufficient_funds"]


async def test_scope_is_checked_again_at_send(
    client, async_session, email_mock
) -> None:
    """The owner can narrow the scope while the model writes."""
    _org, _agent, _domain, inbound = await _seed(
        async_session, scope="members", sender="stranger@example.net"
    )
    data = await _reply(client, inbound.id)
    assert data == {
        "ok": False,
        "state": "skipped",
        "reason": "sender_not_member",
        "reply_id": None,
        "retry_after_seconds": None,
    }
    email_mock.send_email.assert_not_awaited()


async def test_a_gray_verdict_never_gets_a_member_reply(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, dmarc="GRAY")
    assert (await _reply(client, inbound.id))["reason"] == "sender_not_verified"


async def test_a_suppressed_sender_gets_no_reply(
    client, async_session, email_mock
) -> None:
    org, _agent, _domain, inbound = await _seed(async_session)
    async_session.add(
        Suppression(
            organization_id=org,
            channel="email",
            recipient=MEMBER,
            reason="test",
            source="manual",
        )
    )
    await async_session.commit()
    data = await _reply(client, inbound.id)
    assert data["state"] == "skipped"
    email_mock.send_email.assert_not_awaited()


async def test_a_route_removed_while_writing_skips(
    client, async_session, email_mock
) -> None:
    _org, _agent, domain, inbound = await _seed(async_session)
    domain.email_agent_id = None
    await async_session.commit()
    assert (await _reply(client, inbound.id))["reason"] == "no_agent"


async def test_a_muted_agent_skips(client, async_session, email_mock) -> None:
    _org, agent, _domain, inbound = await _seed(async_session)
    agent.email_enabled = False
    await async_session.commit()
    assert (await _reply(client, inbound.id))["reason"] == "no_agent"


async def test_the_thread_cap_holds_at_send(client, async_session, email_mock) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    for _ in range(email_agent.MAX_REPLIES_PER_THREAD):
        async_session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=[MEMBER],
                subject="Re: Question",
                body_text="Hello",
                status="sent",
                metadata_={"reply_to_email_id": str(uuid.uuid4())},
            )
        )
    await async_session.commit()
    assert (await _reply(client, inbound.id))["reason"] == "thread_cap"


def _ses_error(name: str) -> Exception:
    """``deliver_email`` reports a failure by its class name."""
    return type(name, (Exception,), {})("ses")


@pytest.mark.parametrize(
    "name",
    [
        "TooManyRequestsException",
        "LimitExceededException",
        "ConnectTimeoutError",
        "EndpointConnectionError",
    ],
)
async def test_a_failure_that_proves_nothing_was_sent_is_retried(
    client, async_session, email_mock, name
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    send = email_mock.send_email.side_effect
    email_mock.send_email.side_effect = _ses_error(name)
    first = await _reply(client, inbound.id)
    assert first["state"] == "retry" and first["reason"] == name
    assert first["retry_after_seconds"] is None

    email_mock.send_email.side_effect = send
    raw = json.dumps({"email_id": str(inbound.id)}).encode()
    resp = await client.post(
        "/internal/agent/reply-email", content=raw, headers=_signed(raw)
    )
    assert resp.json()["state"] == "done"
    assert resp.json()["reply_id"] == first["reply_id"]
    assert email_mock.send_email.await_args.kwargs["body_text"] == "Happy to help."
    assert email_mock.send_email.await_count == 2


@pytest.mark.parametrize(
    "name",
    ["ReadTimeoutError", "ConnectionClosedError", "MessageRejected", "RuntimeError"],
)
async def test_a_send_that_may_have_gone_out_is_final(
    client, async_session, email_mock, name
) -> None:
    """SES may have sent it: a resend could deliver the reply twice."""
    _org, _agent, _domain, inbound = await _seed(async_session)
    email_mock.send_email.side_effect = _ses_error(name)
    first = await _reply(client, inbound.id)
    assert first["state"] == "failed" and first["reason"] == name

    second = await _reply(client, inbound.id)
    assert second["state"] == "failed" and second["reason"] == name
    assert second["reply_id"] == first["reply_id"]
    assert email_mock.send_email.await_count == 1


async def test_a_resend_keeps_the_same_rule(client, async_session, email_mock) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _prior_reply(
        async_session, org, agent, domain, inbound, status="failed", age=timedelta(0)
    )
    email_mock.send_email.side_effect = _ses_error("ReadTimeoutError")
    assert (await _reply(client, inbound.id))["state"] == "failed"


async def test_unsigned_requests_are_refused(client, async_session) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    resp = await client.post(
        "/internal/agent/reply-email",
        content=_payload(inbound.id),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (401, 403)


def _tool(inbound_id, **extra) -> bytes:
    fields = {
        "inbound_email_id": str(inbound_id),
        "tool_invocation_id": str(uuid.uuid4()),
        "recipient_name": "Ada Lovelace",
        **extra,
    }
    return json.dumps(fields).encode()


async def _tool_email(client, inbound_id, **extra):
    fields = {"subject": "Note", "body_text": "Hello.", **extra}
    raw = _tool(inbound_id, **fields)
    resp = await client.post(
        "/internal/agent/email-run/send-email", content=raw, headers=_signed(raw)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _tool_sms(client, inbound_id, **extra):
    fields = {"body": "Hello.", **extra}
    raw = _tool(inbound_id, **fields)
    resp = await client.post(
        "/internal/agent/email-run/send-sms", content=raw, headers=_signed(raw)
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_tool_email_reaches_a_member_by_name(
    client, async_session, email_mock
) -> None:
    _org, agent, _domain, inbound = await _seed(async_session)
    data = await _tool_email(client, inbound.id)
    assert data == {"ok": True, "spoken": "Email sent."}
    kwargs = email_mock.send_email.await_args.kwargs
    assert kwargs["to_addresses"] == [MEMBER]
    assert kwargs["headers"]["Auto-Submitted"] == "auto-generated"
    row = (
        await async_session.execute(
            select(Email).where(Email.subject == "Note", Email.direction == "outbound")
        )
    ).scalar_one()
    assert row.agent_id == agent.id
    assert row.metadata_["inbound_email_id"] == str(inbound.id)


async def test_tool_sms_reaches_a_member_by_name(
    client, async_session, sms_mock, add_phone_number
) -> None:
    org, _agent, _domain, inbound = await _seed(async_session)
    await add_phone_number(async_session, org, "+14155550100")
    data = await _tool_sms(client, inbound.id)
    assert data == {"ok": True, "spoken": "Text message sent."}
    assert sms_mock.send_sms.await_args.kwargs["to_e164"] == "+33612345678"


async def test_a_retried_run_does_not_send_twice(
    client, async_session, email_mock
) -> None:
    """The tool sends the same id for the same message."""
    _org, _agent, _domain, inbound = await _seed(async_session)
    invocation = str(uuid.uuid4())
    await _tool_email(client, inbound.id, tool_invocation_id=invocation)
    data = await _tool_email(client, inbound.id, tool_invocation_id=invocation)
    assert data == {"ok": True, "spoken": "Email sent."}
    assert email_mock.send_email.await_count == 1


async def test_a_retry_learns_its_send_took_the_last_slot(
    client, async_session, email_mock
) -> None:
    """The client retries a slow send, and that send filled the thread cap."""
    org, agent, domain, inbound = await _seed(async_session)
    await _fill_cap(async_session, org, agent, domain, inbound, own=0)
    row = (
        (await async_session.execute(select(Email).where(Email.subject == "Note")))
        .scalars()
        .first()
    )
    invocation = str(uuid.uuid4())
    row.metadata_ = {**row.metadata_, "tool_invocation_id": invocation}
    await async_session.commit()
    data = await _tool_email(client, inbound.id, tool_invocation_id=invocation)
    assert data == {"ok": True, "spoken": "Email sent."}
    email_mock.send_email.assert_not_awaited()


async def test_a_retry_learns_its_send_outlived_the_claim(
    client, async_session, email_mock, sms_mock, add_phone_number
) -> None:
    org, _agent, _domain, inbound = await _seed(async_session)
    await add_phone_number(async_session, org, "+14155550100")
    email_id, sms_id = str(uuid.uuid4()), str(uuid.uuid4())
    await _tool_email(client, inbound.id, tool_invocation_id=email_id)
    await _tool_sms(client, inbound.id, tool_invocation_id=sms_id)
    inbound.agent_reply_state = "done"
    await async_session.commit()
    assert await _tool_email(client, inbound.id, tool_invocation_id=email_id) == {
        "ok": True,
        "spoken": "Email sent.",
    }
    assert await _tool_sms(client, inbound.id, tool_invocation_id=sms_id) == {
        "ok": True,
        "spoken": "Text message sent.",
    }
    assert email_mock.send_email.await_count == 1
    assert sms_mock.send_sms.await_count == 1


async def test_two_messages_to_one_person_both_go(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    await _tool_email(client, inbound.id, subject="First")
    await _tool_email(client, inbound.id, subject="Second")
    assert email_mock.send_email.await_count == 2


async def test_two_texts_to_one_person_both_go(
    client, async_session, sms_mock, add_phone_number
) -> None:
    org, _agent, _domain, inbound = await _seed(async_session)
    await add_phone_number(async_session, org, "+14155550100")
    await _tool_sms(client, inbound.id, body="First")
    await _tool_sms(client, inbound.id, body="Second")
    assert sms_mock.send_sms.await_count == 2


async def test_a_failed_send_can_be_corrected(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    send = email_mock.send_email.side_effect
    email_mock.send_email.side_effect = RuntimeError("down")
    assert (await _tool_email(client, inbound.id, subject="First"))["ok"] is False
    email_mock.send_email.side_effect = send
    assert (await _tool_email(client, inbound.id, subject="Better"))["ok"] is True


async def test_tool_sends_stop_at_the_limit(client, async_session, email_mock) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    for i in range(email_agent.MAX_TOOL_SENDS):
        async_session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=[f"other{i}@example.com"],
                subject="Note",
                body_text="Hello",
                status="sent",
                metadata_={"inbound_email_id": str(inbound.id)},
            )
        )
    await async_session.commit()
    data = await _tool_email(client, inbound.id)
    assert data == {
        "ok": False,
        "spoken": "That is the limit of messages for one email.",
    }
    email_mock.send_email.assert_not_awaited()


async def test_a_send_ses_never_took_does_not_use_the_thread_cap(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    for _ in range(email_agent.MAX_REPLIES_PER_THREAD):
        async_session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=[MEMBER],
                subject="Note",
                body_text="Hello",
                status="failed",
                end_reason="TooManyRequestsException",
                metadata_={"reply_to_email_id": str(uuid.uuid4())},
            )
        )
    await async_session.commit()
    assert (await _reply(client, inbound.id))["ok"] is True


async def test_a_send_ses_never_took_does_not_use_the_limit(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    for i in range(email_agent.MAX_TOOL_SENDS):
        async_session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=[f"other{i}@example.com"],
                subject="Note",
                body_text="Hello",
                status="failed",
                end_reason="TooManyRequestsException",
                metadata_={"inbound_email_id": str(inbound.id)},
            )
        )
    await async_session.commit()
    assert (await _tool_email(client, inbound.id))["ok"] is True


@pytest.mark.parametrize("state", ["pending", "done", "skipped", "failed", None])
async def test_tools_run_only_during_a_claim(
    client, async_session, email_mock, state
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, state=state)
    data = await _tool_email(client, inbound.id)
    assert data["ok"] is False
    email_mock.send_email.assert_not_awaited()


@pytest.mark.parametrize("verdict", ["GRAY", "PROCESSING_FAILED", None])
async def test_the_server_refuses_tools_for_an_unproven_sender(
    client, async_session, email_mock, sms_mock, add_phone_number, verdict
) -> None:
    """A plain reply is allowed under anyone. The tools are not, whatever the
    worker sends."""
    org, _agent, _domain, inbound = await _seed(
        async_session, scope="anyone", dmarc=verdict, sender="stranger@example.net"
    )
    await add_phone_number(async_session, org, "+14155550100")
    for response in (
        await _tool_email(client, inbound.id),
        await _tool_sms(client, inbound.id),
    ):
        assert response == {
            "ok": False,
            "spoken": "This email can no longer send messages.",
        }
    email_mock.send_email.assert_not_awaited()
    sms_mock.send_sms.assert_not_awaited()


@pytest.mark.parametrize("verdict", ["GRAY", "PROCESSING_FAILED", None])
async def test_an_unproven_sender_gets_no_reply_under_anyone(
    client, async_session, email_mock, verdict
) -> None:
    _org, _agent, _domain, inbound = await _seed(
        async_session, scope="anyone", dmarc=verdict, sender="stranger@example.net"
    )
    data = await _reply(client, inbound.id)
    assert data["state"] == "skipped" and data["reason"] == "sender_not_verified"
    email_mock.send_email.assert_not_awaited()


async def test_a_pass_alone_is_not_the_stamp(client, async_session, email_mock) -> None:
    """Only ingest proves a sender."""
    _org, _agent, _domain, inbound = await _seed(async_session)
    inbound.from_verified = False
    await async_session.commit()
    assert (await _tool_email(client, inbound.id))["ok"] is False
    assert (await _reply(client, inbound.id))["reason"] == "sender_not_verified"
    email_mock.send_email.assert_not_awaited()


async def test_a_failed_dmarc_gets_no_reply_under_anyone(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(
        async_session, scope="anyone", dmarc="FAIL", sender="stranger@example.net"
    )
    assert (await _reply(client, inbound.id))["reason"] == "sender_not_verified"
    email_mock.send_email.assert_not_awaited()


async def test_a_proven_outside_sender_keeps_the_tools(
    client, async_session, email_mock
) -> None:
    """The owner asked for full tools under anyone, for a proven address."""
    _org, _agent, _domain, inbound = await _seed(
        async_session, scope="anyone", sender="stranger@example.net"
    )
    assert (await _tool_email(client, inbound.id))["ok"] is True


async def test_tool_sends_stop_at_the_thread_cap(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    for _ in range(email_agent.MAX_REPLIES_PER_THREAD):
        async_session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=["other@example.com"],
                subject="Note",
                body_text="Hello",
                status="sent",
                metadata_={"thread_sender": MEMBER},
            )
        )
    await async_session.commit()
    data = await _tool_email(client, inbound.id)
    assert data == {
        "ok": False,
        "spoken": "That is the limit of messages for one email.",
    }
    email_mock.send_email.assert_not_awaited()


async def _fill_cap(session, org, agent, domain, inbound, *, own):
    """Use every slot of the sender's cap. ``own`` tool sends belong to this run."""
    for i in range(email_agent.MAX_REPLIES_PER_THREAD):
        meta = {"thread_sender": MEMBER}
        if i < own:
            meta["inbound_email_id"] = str(inbound.id)
        session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                agent_id=agent.id,
                from_address=ADDRESS,
                to_addresses=["other@example.com"],
                subject="Note",
                body_text="Hello",
                status="sent",
                metadata_=meta,
            )
        )
    await session.commit()


async def test_a_runs_own_tool_sends_do_not_stop_its_reply(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _fill_cap(async_session, org, agent, domain, inbound, own=1)
    # The others used 19 slots, and this run's tool send took the last one.
    assert (await _reply(client, inbound.id))["state"] == "done"
    email_mock.send_email.assert_awaited_once()


async def test_a_full_cap_from_other_runs_still_stops_the_reply(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _fill_cap(async_session, org, agent, domain, inbound, own=0)
    assert (await _reply(client, inbound.id))["reason"] == "thread_cap"


async def test_a_runs_own_sends_still_stop_more_tool_sends(
    client, async_session, email_mock
) -> None:
    org, agent, domain, inbound = await _seed(async_session)
    await _fill_cap(async_session, org, agent, domain, inbound, own=1)
    data = await _tool_email(client, inbound.id)
    assert data["spoken"] == "That is the limit of messages for one email."
    email_mock.send_email.assert_not_awaited()


async def test_a_tool_send_records_its_thread(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    await _tool_email(client, inbound.id)
    row = (
        await async_session.execute(select(Email).where(Email.subject == "Note"))
    ).scalar_one()
    assert row.metadata_["thread_sender"] == MEMBER


async def test_a_custom_domain_reply_ignores_the_to_header(
    client, async_session, email_mock
) -> None:
    _org, _agent, domain, inbound = await _seed(async_session)
    domain.kind = "custom"
    domain.domain = "acme.com"
    domain.local_prefix_user = None
    domain.local_prefix_org = None
    inbound.to_addresses = ["ceo@acme.com"]
    await async_session.commit()
    await _reply(client, inbound.id)
    assert email_mock.send_email.await_args.kwargs["from_address"] == "noreply@acme.com"


async def test_tools_read_the_scope_again(client, async_session, email_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(
        async_session, sender="stranger@example.net"
    )
    data = await _tool_email(client, inbound.id)
    assert data["ok"] is False
    email_mock.send_email.assert_not_awaited()


async def test_tool_refuses_without_funds(client, async_session, email_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, credits=0)
    assert (await _tool_email(client, inbound.id))["ok"] is False
    email_mock.send_email.assert_not_awaited()


async def test_tool_says_when_a_name_is_unknown(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    data = await _tool_email(client, inbound.id, recipient_name="Nobody")
    assert "couldn't find Nobody" in data["spoken"]


async def test_tool_sms_with_no_phone_says_so(client, async_session, sms_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, phone=None)
    data = await _tool_sms(client, inbound.id)
    assert data["ok"] is False and "couldn't find a number" in data["spoken"]


async def test_tool_sms_refuses_a_name_two_members_share(
    client, async_session, sms_mock
) -> None:
    """Only one has a phone, but the model may mean the other."""
    org, _agent, _domain, inbound = await _seed(async_session)
    uid = uuid.uuid4()
    async_session.add(
        User(
            id=uid,
            name="Ada Lovelace",
            email="other-ada@example.com",
            email_verified=True,
            created_at=NOW,
        )
    )
    async_session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=uid,
            organization_id=org,
            role="member",
            created_at=NOW,
        )
    )
    await async_session.commit()
    data = await _tool_sms(client, inbound.id)
    assert data["ok"] is False and "More than one person" in data["spoken"]


async def test_the_raw_request_cannot_name_an_address(client, async_session) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    raw = _tool(inbound.id, subject="s", body_text="b", to="x@evil.example")
    resp = await client.post(
        "/internal/agent/email-run/send-email", content=raw, headers=_signed(raw)
    )
    assert resp.status_code == 422


async def test_sends_count_across_channels(async_session) -> None:
    org, agent, _domain, inbound = await _seed(async_session)
    async_session.add(
        Sms(
            organization_id=org,
            provider="twilio",
            from_e164="+14155550100",
            to_e164="+33612345678",
            direction="outbound",
            status="sent",
            body="x",
            agent_id=agent.id,
            metadata_={"inbound_email_id": str(inbound.id)},
        )
    )
    await async_session.commit()
    assert await email_agent.tool_sends_for_reply(async_session, inbound) == 1
