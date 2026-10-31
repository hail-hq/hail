"""POST /internal/agent/reply-email and the email-run tool routes: an agent's
reply goes out threaded, gated like every other send, and tools stay bounded."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
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
from sqlalchemy import select
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


async def test_a_retry_does_not_send_twice(client, async_session, email_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    first = await _reply(client, inbound.id)
    second = await _reply(client, inbound.id)
    assert second["reply_id"] == first["reply_id"]
    assert email_mock.send_email.await_count == 1


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


async def test_a_provider_failure_reports_failed(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    email_mock.send_email.side_effect = RuntimeError("down")
    data = await _reply(client, inbound.id)
    assert data["state"] == "failed" and data["reason"] == "RuntimeError"


async def test_unsigned_requests_are_refused(client, async_session) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    resp = await client.post(
        "/internal/agent/reply-email",
        content=_payload(inbound.id),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (401, 403)


def _tool(inbound_id, **extra) -> bytes:
    return json.dumps(
        {
            "inbound_email_id": str(inbound_id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "Ada Lovelace",
            **extra,
        }
    ).encode()


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
    assert "Auto-Submitted" not in (kwargs["headers"] or {})
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
    _org, _agent, _domain, inbound = await _seed(async_session)
    await _tool_email(client, inbound.id)
    data = await _tool_email(client, inbound.id, subject="Note again")
    assert data["ok"] is True
    assert email_mock.send_email.await_count == 1


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


@pytest.mark.parametrize("state", ["pending", "done", "skipped", "failed", None])
async def test_tools_run_only_during_a_claim(
    client, async_session, email_mock, state
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, state=state)
    data = await _tool_email(client, inbound.id)
    assert data["ok"] is False
    email_mock.send_email.assert_not_awaited()


async def test_tools_read_the_scope_again(client, async_session, email_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(
        async_session, sender="stranger@example.net"
    )
    data = await _tool_email(client, inbound.id)
    assert data["ok"] is False
    email_mock.send_email.assert_not_awaited()


async def test_tool_unknown_name_and_no_funds(
    client, async_session, email_mock
) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, credits=0)
    assert (await _tool_email(client, inbound.id))["ok"] is False
    email_mock.send_email.assert_not_awaited()
    raw = _tool(inbound.id, subject="s", body_text="b")
    raw = raw.replace(b"Ada Lovelace", b"Nobody")
    resp = await client.post(
        "/internal/agent/email-run/send-email", content=raw, headers=_signed(raw)
    )
    assert "couldn't find Nobody" in resp.json()["spoken"]


async def test_tool_sms_with_no_phone_says_so(client, async_session, sms_mock) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session, phone=None)
    data = await _tool_sms(client, inbound.id)
    assert data["ok"] is False and "couldn't find a number" in data["spoken"]


async def test_the_raw_request_cannot_name_an_address(client, async_session) -> None:
    _org, _agent, _domain, inbound = await _seed(async_session)
    raw = _tool(inbound.id, subject="s", body_text="b", to="x@evil.example")
    resp = await client.post(
        "/internal/agent/email-run/send-email", content=raw, headers=_signed(raw)
    )
    assert resp.status_code == 422


async def test_sends_count_across_channels(async_session) -> None:
    org, _agent, _domain, inbound = await _seed(async_session)
    async_session.add(
        Sms(
            organization_id=org,
            provider="twilio",
            from_e164="+14155550100",
            to_e164="+33612345678",
            direction="outbound",
            status="sent",
            body="x",
            metadata_={"inbound_email_id": str(inbound.id)},
        )
    )
    await async_session.commit()
    assert await email_agent.tool_sends_for_reply(async_session, inbound.id) == 1
