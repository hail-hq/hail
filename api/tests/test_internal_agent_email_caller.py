"""POST /internal/agent/email-caller: email the person on the call.

Opt-in per agent, one address and at most two emails per call, the normal
email gate and sender. Every send here goes to the mocked email provider.
"""

from __future__ import annotations

import json
import uuid

import pytest
from botocore.exceptions import ConnectTimeoutError, ReadTimeoutError
from hailhq.core import hmac_signing
from hailhq.core.agent_tools import email_caller
from hailhq.core.billing import CALL_META_BILLED
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.config import settings
from hailhq.core.models import (
    Agent,
    AuditLog,
    Call,
    CallEvent,
    Email,
    EmailDomain,
    PhoneNumber,
)
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

HMAC_SECRET = "test-internal-secret"
PATH = "/internal/agent/email-caller"


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, HMAC_SECRET),
    }


async def _call(
    session: AsyncSession,
    add_phone_number,
    *,
    enabled: bool = True,
    with_agent: bool = True,
    billed: bool = False,
    transport: str | None = None,
    like: Call | None = None,
    to_e164: str = "+14155550123",
) -> Call:
    """A live outbound call. ``like``: another call of the same org and agent."""
    org = like.organization_id if like else uuid.uuid4()
    agent = await session.get(Agent, like.agent_id) if like else None
    if with_agent and agent is None:
        agent = Agent(
            organization_id=org,
            name="Front desk",
            system_prompt="Help.",
            email_caller_enabled=enabled,
        )
        session.add(agent)
    if like is None:
        session.add(
            EmailDomain(
                organization_id=org,
                kind="custom",
                domain="mail.a.test",
                verification_status="verified",
            )
        )
    await session.flush()
    number = (
        await session.get(PhoneNumber, like.from_number_id)
        if like
        else await add_phone_number(session, org)
    )
    call = Call(
        organization_id=org,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164=to_e164,
        status="in_progress",
        agent_id=agent.id if agent else None,
        voice_config={},
        metadata_={CALL_META_BILLED: billed},
    )
    if transport == "browser":
        # A browser test call has no phone numbers.
        call.transport = "browser"
        call.from_number_id = None
        call.from_e164 = None
        call.to_e164 = None
    session.add(call)
    await session.commit()
    return call


async def _post(client, call: Call, to="sam@example.com", invocation=None):
    body = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(invocation or uuid.uuid4()),
            "to_address": to,
            "subject": "Datasheet",
            "body_text": "Here it is: https://example.com/ds.pdf",
            "org_name": "Acme Dental",
        }
    ).encode()
    resp = await client.post(PATH, content=body, headers=_signed(body))
    assert resp.status_code == 200, resp.text
    return resp.json()


async def _emails(session: AsyncSession) -> list[Email]:
    return list((await session.execute(select(Email))).scalars().all())


async def test_off_by_default_sends_nothing(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number, enabled=False)
    data = await _post(client, call)
    assert data["ok"] is False
    assert await _emails(async_session) == []
    email_mock.send_email.assert_not_awaited()


async def test_new_agents_start_with_it_off(async_session):
    agent = Agent(organization_id=uuid.uuid4(), name="A", system_prompt="Help.")
    async_session.add(agent)
    await async_session.commit()
    await async_session.refresh(agent)
    assert agent.email_caller_enabled is False


async def test_on_sends_to_the_caller_from_the_verified_domain(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    data = await _post(client, call, to="Sam@Example.com")
    assert data == {"ok": True, "spoken": "Email sent."}

    (row,) = await _emails(async_session)
    assert row.to_addresses == ["Sam@Example.com"]
    assert row.cc_addresses in (None, [])
    assert row.bcc_addresses in (None, [])
    assert row.from_address == "noreply@mail.a.test"
    assert row.subject == "From a phone call with Acme Dental: Datasheet"
    # None, as for other call sends. History leaves it out by its mark.
    assert row.agent_id is None
    assert row.metadata_["call_id"] == str(call.id)
    assert row.metadata_["email_caller"] is True
    assert row.metadata_["caller_e164"] == "+14155550123"
    # Fixed lines the agent cannot change, around its text.
    assert row.body_text.startswith(
        "You asked for this during a phone call with Acme Dental.\n\n"
        "Here it is: https://example.com/ds.pdf\n\n--\n"
    )
    assert "If that was not you, you can ignore it." in row.body_text
    assert row.status == "sent"
    email_mock.send_email.assert_awaited_once()

    (event,) = (
        (
            await async_session.execute(
                select(CallEvent).where(
                    CallEvent.call_id == call.id,
                    CallEvent.kind == email_caller.EVENT_KIND,
                )
            )
        )
        .scalars()
        .all()
    )
    assert event.payload["email_id"] == str(row.id)
    assert "to" not in event.payload  # the address stays on the email row
    assert event.payload["status"] == "sent"


async def test_two_emails_per_call_then_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    assert (await _post(client, call))["ok"] is True
    # Case-insensitive: the same address said again.
    assert (await _post(client, call, to="SAM@example.com"))["ok"] is True
    third = await _post(client, call)
    assert third["ok"] is False
    assert "limit" in third["spoken"]
    assert len(await _emails(async_session)) == email_caller.MAX_EMAILS_PER_CALL


async def test_a_second_address_on_the_call_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    assert (await _post(client, call))["ok"] is True
    other = await _post(client, call, to="someone.else@example.com")
    assert other["ok"] is False
    assert "already used" in other["spoken"]
    assert [e.to_addresses for e in await _emails(async_session)] == [
        ["sam@example.com"]
    ]


async def test_control_characters_are_cleaned_from_the_subject(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    body = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "to_address": "sam@example.com",
            "subject": "Data\u202esheet\r\nBcc: x@example.com",
            "body_text": "Link.",
            "org_name": "Acme Dental",
        }
    ).encode()
    resp = await client.post(PATH, content=body, headers=_signed(body))
    assert resp.json()["ok"] is True
    (row,) = await _emails(async_session)
    assert row.subject == (
        "From a phone call with Acme Dental: Datasheet Bcc: x@example.com"
    )


async def test_a_subject_of_only_control_characters_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    body = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "to_address": "sam@example.com",
            "subject": "\u200b\r\n\t",
            "body_text": "Link.",
        }
    ).encode()
    resp = await client.post(PATH, content=body, headers=_signed(body))
    assert resp.json()["ok"] is False
    assert await _emails(async_session) == []


async def test_a_failed_send_with_no_reason_counts(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    assert (await _post(client, call))["ok"] is True
    (row,) = await _emails(async_session)
    row.status = "failed"
    row.end_reason = None
    await async_session.commit()
    assert (await _post(client, call))["ok"] is True
    assert (await _post(client, call))["ok"] is False


async def test_a_failed_send_does_not_count(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    happy = email_mock.send_email.side_effect
    # SES surely never took it: the send is free.
    email_mock.send_email.side_effect = ConnectTimeoutError(endpoint_url="ses")
    assert (await _post(client, call))["ok"] is False
    email_mock.send_email.side_effect = happy
    assert (await _post(client, call))["ok"] is True
    assert (await _post(client, call))["ok"] is True
    statuses = sorted(e.status for e in await _emails(async_session))
    assert statuses == ["failed", "sent", "sent"]
    kinds = (
        (
            await async_session.execute(
                select(CallEvent.payload).where(
                    CallEvent.call_id == call.id,
                    CallEvent.kind == email_caller.EVENT_KIND,
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(p["status"] for p in kinds) == ["failed", "sent", "sent"]


async def test_a_send_that_may_have_gone_out_counts(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    happy = email_mock.send_email.side_effect
    # A read timeout: SES may have sent it, so it uses a slot.
    email_mock.send_email.side_effect = ReadTimeoutError(endpoint_url="ses")
    assert (await _post(client, call))["ok"] is False
    email_mock.send_email.side_effect = happy
    assert (await _post(client, call))["ok"] is True
    assert (await _post(client, call))["ok"] is False


async def test_suppressed_address_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    await add_suppression(
        async_session,
        organization_id=call.organization_id,
        recipient="sam@example.com",
        channel="email",
        reason="unsubscribe",
        source="unsubscribe_link",
    )
    await async_session.commit()
    data = await _post(client, call)
    assert data["ok"] is False
    assert "suppressed" not in data["spoken"]  # never reveal list membership
    assert await _emails(async_session) == []
    email_mock.send_email.assert_not_awaited()
    blocked = (
        (
            await async_session.execute(
                select(AuditLog).where(
                    AuditLog.organization_id == call.organization_id,
                    AuditLog.action == "agent.email.blocked",
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(blocked) == 1


async def test_replay_sends_once(client, async_session, email_mock, add_phone_number):
    call = await _call(async_session, add_phone_number)
    invocation = uuid.uuid4()
    assert (await _post(client, call, invocation=invocation))["ok"] is True
    assert (await _post(client, call, invocation=invocation))["ok"] is True
    assert len(await _emails(async_session)) == 1
    email_mock.send_email.assert_awaited_once()
    events = (
        (
            await async_session.execute(
                select(CallEvent.id).where(
                    CallEvent.call_id == call.id,
                    CallEvent.kind == email_caller.EVENT_KIND,
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(events) == 1  # the retry does not write a second event


async def test_call_without_an_agent_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number, with_agent=False)
    assert (await _post(client, call))["ok"] is False
    assert await _emails(async_session) == []


async def test_switching_it_off_stops_a_live_call(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    agent = await async_session.get(Agent, call.agent_id)
    agent.email_caller_enabled = False
    await async_session.commit()
    assert (await _post(client, call))["ok"] is False
    assert await _emails(async_session) == []


async def test_browser_test_call_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number, transport="browser")
    assert (await _post(client, call))["ok"] is False
    assert await _emails(async_session) == []


async def test_billed_call_without_credit_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number, billed=True)
    assert (await _post(client, call))["ok"] is False
    assert await _emails(async_session) == []


async def test_ended_call_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    call.status = "completed"
    call.end_reason = "normal_hangup"
    await async_session.commit()
    assert (await _post(client, call))["ok"] is False


@pytest.mark.parametrize(
    "extra",
    [
        {"to_address": "not an address"},
        {"to_address": "bob\u200b@example.com"},
        {"to_address": "a@example.com, b@example.com"},
        {"to_address": "a@b.test", "cc": ["c@d.test"]},
        {"to_address": "a@b.test", "attachment_ids": [str(uuid.uuid4())]},
    ],
)
async def test_bad_input_is_rejected(client, extra):
    body = json.dumps(
        {
            "call_id": str(uuid.uuid4()),
            "tool_invocation_id": str(uuid.uuid4()),
            "subject": "s",
            "body_text": "b",
            **extra,
        }
    ).encode()
    resp = await client.post(PATH, content=body, headers=_signed(body))
    assert resp.status_code == 422


async def test_caller_number_gets_three_emails_a_day_across_calls(
    client, async_session, email_mock, add_phone_number
):
    first = await _call(async_session, add_phone_number)
    assert (await _post(client, first))["ok"] is True
    assert (await _post(client, first))["ok"] is True
    second = await _call(async_session, add_phone_number, like=first)
    assert (await _post(client, second))["ok"] is True
    third = await _call(async_session, add_phone_number, like=first)
    data = await _post(client, third)
    assert data["ok"] is False
    assert "today" in data["spoken"]
    assert len(await _emails(async_session)) == 3
    # Another person calling the same org still gets an email.
    other = await _call(
        async_session, add_phone_number, like=first, to_e164="+14155550199"
    )
    assert (await _post(client, other, to="other@example.com"))["ok"] is True


async def test_caller_number_gets_two_addresses_a_day(
    client, async_session, email_mock, add_phone_number
):
    first = await _call(async_session, add_phone_number)
    assert (await _post(client, first, to="a@example.com"))["ok"] is True
    second = await _call(async_session, add_phone_number, like=first)
    assert (await _post(client, second, to="b@example.com"))["ok"] is True
    # A third address from the same caller, on a new call, is refused.
    third = await _call(async_session, add_phone_number, like=first)
    data = await _post(client, third, to="c@example.com")
    assert data["ok"] is False
    assert "another address" in data["spoken"]
    reasons = (
        (
            await async_session.execute(
                select(AuditLog.payload["reason"].astext).where(
                    AuditLog.organization_id == first.organization_id,
                    AuditLog.action == "agent.email.blocked",
                )
            )
        )
        .scalars()
        .all()
    )
    assert reasons == ["caller_daily_addresses"]
    assert sorted(e.to_addresses[0] for e in await _emails(async_session)) == [
        "a@example.com",
        "b@example.com",
    ]


async def test_org_daily_cap(
    client, async_session, email_mock, add_phone_number, monkeypatch
):
    monkeypatch.setattr(email_caller, "MAX_EMAILS_PER_ORG_PER_DAY", 2)
    first = await _call(async_session, add_phone_number, to_e164="+14155550101")
    assert (await _post(client, first))["ok"] is True
    second = await _call(
        async_session, add_phone_number, like=first, to_e164="+14155550102"
    )
    assert (await _post(client, second))["ok"] is True
    third = await _call(
        async_session, add_phone_number, like=first, to_e164="+14155550103"
    )
    assert (await _post(client, third))["ok"] is False
    assert len(await _emails(async_session)) == 2
    reasons = (
        (
            await async_session.execute(
                select(AuditLog.payload["reason"].astext).where(
                    AuditLog.organization_id == first.organization_id,
                    AuditLog.action == "agent.email.blocked",
                )
            )
        )
        .scalars()
        .all()
    )
    assert reasons == ["org_daily_cap"]


async def test_a_bounced_address_can_be_corrected_on_the_call(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    assert (await _post(client, call, to="sam@exmple.com"))["ok"] is True
    (row,) = await _emails(async_session)
    row.status = "bounced"
    await async_session.commit()
    assert (await _post(client, call, to="sam@example.com"))["ok"] is True
    # The bounce still used one of the two emails for this call.
    assert (await _post(client, call, to="sam@example.com"))["ok"] is False


async def test_hidden_caller_number_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number, to_e164="anonymous")
    assert (await _post(client, call))["ok"] is False
    assert await _emails(async_session) == []


async def test_a_blank_body_is_refused(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    body = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "to_address": "sam@example.com",
            "subject": "Datasheet",
            "body_text": "   ",
        }
    ).encode()
    resp = await client.post(PATH, content=body, headers=_signed(body))
    assert resp.json()["ok"] is False
    assert await _emails(async_session) == []


async def test_paused_agent_sends_nothing(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    agent = await async_session.get(Agent, call.agent_id)
    agent.status = "paused"
    await async_session.commit()
    assert (await _post(client, call))["ok"] is False
    assert await _emails(async_session) == []


async def test_org_name_falls_back_to_the_organizations_table(
    client, async_session, email_mock, add_phone_number
):
    call = await _call(async_session, add_phone_number)
    await async_session.execute(
        text("INSERT INTO organizations (id, name) VALUES (:id, :name)"),
        {"id": call.organization_id, "name": "Acme Dental"},
    )
    await async_session.commit()
    body = json.dumps(
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "to_address": "sam@example.com",
            "subject": "Datasheet",
            "body_text": "Link.",
        }
    ).encode()
    resp = await client.post(PATH, content=body, headers=_signed(body))
    assert resp.json()["ok"] is True
    (row,) = await _emails(async_session)
    assert row.subject == "From a phone call with Acme Dental: Datasheet"


async def test_one_address_gets_three_emails_a_day_from_any_caller(
    client, async_session, email_mock, add_phone_number
):
    first = await _call(async_session, add_phone_number, to_e164="+14155550101")
    assert (await _post(client, first, to="victim@example.com"))["ok"] is True
    for n in (2, 3):
        call = await _call(
            async_session, add_phone_number, like=first, to_e164=f"+1415555010{n}"
        )
        assert (await _post(client, call, to="victim@example.com"))["ok"] is True
    spoofed = await _call(
        async_session, add_phone_number, like=first, to_e164="+14155550104"
    )
    data = await _post(client, spoofed, to="victim@example.com")
    assert data["ok"] is False
    assert len(await _emails(async_session)) == 3
    reasons = (
        (
            await async_session.execute(
                select(AuditLog.payload["reason"].astext).where(
                    AuditLog.organization_id == first.organization_id,
                    AuditLog.action == "agent.email.blocked",
                )
            )
        )
        .scalars()
        .all()
    )
    assert reasons == ["address_daily_cap"]


async def test_a_second_address_names_the_per_call_rule(
    client, async_session, email_mock, add_phone_number
):
    """Yesterday's address plus this call's makes two; a third on this call
    breaks the per-call rule first, and the refusal says so."""
    first = await _call(async_session, add_phone_number)
    assert (await _post(client, first, to="c@example.com"))["ok"] is True
    second = await _call(async_session, add_phone_number, like=first)
    assert (await _post(client, second, to="a@example.com"))["ok"] is True
    data = await _post(client, second, to="b@example.com")
    assert data["ok"] is False
    assert "already used" in data["spoken"]
