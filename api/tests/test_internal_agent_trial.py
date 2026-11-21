"""Trial texts from agent sends: in-call and email-run ``send_sms``, the
handover notice, and browser conversations. Only the workspace's verified
trial phone gets one, from Hail's trial number, with prefix and footer."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from hailhq.api.routes.internal.agent import (
    _SPOKEN_NOT_ALLOWED,
    _SPOKEN_SMS_SENT,
    _SPOKEN_SMS_UNCONFIGURED,
)
from hailhq.core import hmac_signing
from hailhq.core.agent_tools.send_sms import MAX_BODY_CHARS as SMS_MAX_BODY_CHARS
from hailhq.core.config import settings
from hailhq.core.handover import handover_sms_text
from hailhq.core.models import (
    AccountCredit,
    Agent,
    AgentHandoverContact,
    AgentSendLog,
    AuditLog,
    Call,
    Contact,
    Email,
    EmailDomain,
    Organization,
    OrganizationMember,
    PhoneNumber,
    Sms,
    User,
)
from hailhq.core.sms_ingest import ingest_inbound_sms
from hailhq.core.sms_registration_gate import BLOCK_MESSAGE
from hailhq.core.sms_trial import SMS_TRIAL_META_KEY, TRIAL_FOOTER, TRIAL_PREFIX
from sqlalchemy import select

from .conftest import (
    approve_us_sms,
    seed_sms_trial,
    seed_trial_texts,
    set_sms_trial,
    verify_trial_phone,
)

SECRET = "test-internal-secret"
TRIAL_E164 = "+18005550199"
PHONE = "+14155550142"
OTHER_PHONE = "+14155550143"
OWN_NUMBER = "+14155550144"
COUNTERPART = "+14155550145"
VOICE_NUMBER = "+14155550146"
NOW = datetime(2026, 11, 1, tzinfo=timezone.utc)
MAX_TRIAL_CHARS = SMS_MAX_BODY_CHARS - len(TRIAL_PREFIX) - len(TRIAL_FOOTER)

SITES = ["in_call", "email_run", "handover", "browser"]
REFUSED = {
    "in_call": {"ok": False, "spoken": _SPOKEN_SMS_UNCONFIGURED},
    "email_run": {"ok": False, "spoken": _SPOKEN_SMS_UNCONFIGURED},
    "handover": {"sms": "blocked:us_registration"},
    "browser": {"ok": False, "spoken": _SPOKEN_NOT_ALLOWED},
}
SENT = {
    "in_call": {"ok": True, "spoken": _SPOKEN_SMS_SENT},
    "email_run": {"ok": True, "spoken": "Text message sent."},
    "handover": {"sms": "sent"},
    "browser": {"ok": True, "spoken": _SPOKEN_SMS_SENT},
}


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    # Browser conversations text only when the carrier is configured.
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "test-secret")


async def _post(client, path: str, payload: dict) -> dict:
    raw = json.dumps(payload).encode()
    resp = await client.post(
        path,
        content=raw,
        headers={
            "Content-Type": "application/json",
            "X-Hail-Signature": hmac_signing.sign(raw, SECRET),
        },
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _credit(org) -> AccountCredit:
    return AccountCredit(
        organization_id=org,
        kind="credit",
        channel="credit",
        amount_cents=10_000,
        source="test",
        ref="test",
    )


async def _number(
    s, org, *, registered: bool, capabilities=("voice", "sms"), e164=OWN_NUMBER
):
    number = PhoneNumber(
        organization_id=org,
        e164=e164,
        country_code="US",
        number_type="local",
        capabilities=list(capabilities),
        provider="twilio",
        provider_resource_id=f"PN_{e164}",
        provisioning_state="active",
    )
    s.add(number)
    await s.commit()
    if registered:
        await approve_us_sms(s, org)
        await s.commit()
    return number


async def _in_call(s, org, to, *, registered, body="hi"):
    """An outbound call to ``to`` from a voice-only number. The workspace has
    no text number unless ``registered``."""
    if registered:
        await _number(s, org, registered=True)
    voice = await _number(
        s, org, registered=False, capabilities=("voice",), e164=VOICE_NUMBER
    )
    call = Call(
        organization_id=org,
        from_number_id=voice.id,
        from_e164=voice.e164,
        to_e164=to,
        status="in_progress",
        voice_config={},
        metadata_={},
    )
    s.add(call)
    await s.commit()
    payload = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": body,
    }
    return lambda client: _post(client, "/internal/agent/send-sms", payload)


async def _email_run(s, org, to, *, registered, body="hi"):
    """An email run whose member Ada has the phone ``to``. No text number
    unless ``registered``."""
    if registered:
        await _number(s, org, registered=True)
    s.add(_credit(org))
    agent = Agent(organization_id=org, name="Inbox", system_prompt="Help.")
    s.add(agent)
    await s.flush()
    domain = EmailDomain(
        organization_id=org,
        kind="hail_mail",
        domain=f"inbox-{org.hex[:8]}+acme@mail.hail.so",
        local_prefix_user=f"inbox-{org.hex[:8]}",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=NOW,
        email_agent_id=agent.id,
        email_agent_scope="members",
    )
    s.add(domain)
    await s.flush()
    uid = uuid.uuid4()
    s.add(
        User(
            id=uid,
            name="Ada Lovelace",
            email=f"ada-{uid.hex[:8]}@example.com",
            email_verified=True,
            created_at=NOW,
            phone_number=to,
        )
    )
    s.add(
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
        from_address=f"ada-{uid.hex[:8]}@example.com",
        to_addresses=[domain.domain],
        subject="Question",
        body_text="Can you text me?",
        status="received",
        message_id=f"<{uid.hex}@example.com>",
        dmarc_verdict="PASS",
        from_verified=True,
        agent_id=agent.id,
        agent_reply_state="processing",
    )
    s.add(inbound)
    await s.commit()
    payload = {
        "inbound_email_id": str(inbound.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "recipient_name": "Ada Lovelace",
        "body": body,
    }
    return lambda client: _post(client, "/internal/agent/email-run/send-sms", payload)


async def _handover(
    s, org, to, *, registered, agent_name="Desk", capabilities=("voice", "sms")
):
    """A call whose agent hands over to the contact Sam at ``to``, from the
    workspace's number (unregistered unless ``registered``)."""
    number = await _number(s, org, registered=registered, capabilities=capabilities)
    agent = Agent(organization_id=org, name=agent_name, system_prompt="Help.")
    contact = Contact(organization_id=org, name="Sam", phone_e164=to)
    s.add_all([agent, contact])
    await s.flush()
    s.add(
        AgentHandoverContact(
            agent_id=agent.id, contact_id=contact.id, note="Billing", position=0
        )
    )
    call = Call(
        organization_id=org,
        from_number_id=number.id,
        from_e164=number.e164,
        to_e164=COUNTERPART,
        status="in_progress",
        agent_id=agent.id,
        provider="twilio",
        voice_config={},
    )
    s.add(call)
    await s.commit()
    payload = {
        "tool_invocation_id": str(uuid.uuid4()),
        "call_id": str(call.id),
        "contact_id": str(contact.id),
        "reason": "a leak",
    }
    return lambda client: _post(client, "/internal/agent/handover-sms", payload)


async def _browser(s, org, to, *, registered, demo=False, body="hi"):
    """A console browser conversation texting ``to`` from the workspace's own
    number (unregistered unless ``registered``)."""
    number = await _number(s, org, registered=registered)
    s.add(_credit(org))
    call = Call(
        organization_id=org,
        transport="browser",
        provider="browser",
        direction="inbound",
        status="in_progress",
        max_duration_seconds=300,
        voice_config={},
        metadata_={
            "browser_trial": demo,
            "browser_contacts": {"sms": to},
            "browser_sms_number_id": str(number.id),
        },
    )
    s.add(call)
    await s.commit()
    payload = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": body,
    }
    return lambda client: _post(client, "/internal/agent/send-sms", payload)


SETUP = {
    "in_call": _in_call,
    "email_run": _email_run,
    "handover": _handover,
    "browser": _browser,
}


async def _outbound(s, org) -> list[Sms]:
    return (
        (
            await s.execute(
                select(Sms).where(
                    Sms.organization_id == org,
                    Sms.direction == "outbound",
                    Sms.metadata_.has_key("tool_invocation_id"),
                )
            )
        )
        .scalars()
        .all()
    )


@pytest.mark.parametrize("site", SITES)
@pytest.mark.parametrize(
    "case",
    [
        "not_verified_phone",
        "pending_phone",
        "other_workspace_phone",
        "cap_used_25",
        "feature_off",
    ],
)
async def test_agent_trial_refused(
    client, async_session, sms_mock, monkeypatch, site, case
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    to = PHONE
    if case == "not_verified_phone":
        await verify_trial_phone(async_session, org, PHONE)
        to = OTHER_PHONE
    elif case == "pending_phone":
        await verify_trial_phone(async_session, org, PHONE, status="pending")
    elif case == "other_workspace_phone":
        await verify_trial_phone(async_session, uuid.uuid4(), PHONE)
    elif case == "cap_used_25":
        await verify_trial_phone(async_session, org, PHONE)
        await seed_trial_texts(async_session, org, 25)
    elif case == "feature_off":
        await verify_trial_phone(async_session, org, PHONE)
        await set_sms_trial(async_session, ready=False)
    send = await SETUP[site](async_session, org, to, registered=False)

    assert await send(client) == REFUSED[site]
    sms_mock.send_sms.assert_not_awaited()
    assert await _outbound(async_session, org) == []
    if site == "browser":
        # Nothing commits on a refusal: the request session's rollback drops
        # the test-text reservation, so it does not use one of the three.
        await async_session.rollback()
        call = (
            await async_session.execute(select(Call).where(Call.organization_id == org))
        ).scalar_one()
        assert "browser_actions" not in call.metadata_


@pytest.mark.parametrize("site", SITES)
async def test_agent_trial_sent_with_prefix_and_footer(
    client, async_session, sms_mock, monkeypatch, site
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    send = await SETUP[site](async_session, org, PHONE, registered=False)

    assert await send(client) == SENT[site]
    sent = sms_mock.send_sms.await_args.kwargs
    assert sent["from_e164"] == TRIAL_E164 and sent["to_e164"] == PHONE
    words = (
        handover_sms_text("Desk", COUNTERPART, "a leak") if site == "handover" else "hi"
    )
    assert sent["body"] == TRIAL_PREFIX + words + TRIAL_FOOTER
    [row] = await _outbound(async_session, org)
    assert row.from_e164 == TRIAL_E164 and row.to_e164 == PHONE
    assert row.body == TRIAL_PREFIX + words + TRIAL_FOOTER
    assert row.metadata_[SMS_TRIAL_META_KEY] is True


@pytest.mark.parametrize("site", SITES)
async def test_agent_registered_number_preferred(
    client, async_session, sms_mock, monkeypatch, site
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    send = await SETUP[site](async_session, org, PHONE, registered=True)

    assert await send(client) == SENT[site]
    sent = sms_mock.send_sms.await_args.kwargs
    assert sent["from_e164"] == OWN_NUMBER
    assert not sent["body"].startswith(TRIAL_PREFIX)
    [row] = await _outbound(async_session, org)
    assert SMS_TRIAL_META_KEY not in row.metadata_


@pytest.mark.parametrize("site", ["in_call", "email_run"])
async def test_long_body_refused(client, async_session, sms_mock, monkeypatch, site):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    send = await SETUP[site](
        async_session, org, PHONE, registered=False, body="x" * SMS_MAX_BODY_CHARS
    )

    assert await send(client) == {
        "ok": False,
        "spoken": f"Keep trial texts to {MAX_TRIAL_CHARS} characters. "
        "Nothing was sent.",
    }
    sms_mock.send_sms.assert_not_awaited()
    assert await _outbound(async_session, org) == []


async def test_in_call_unregistered_number_uses_trial_and_keeps_the_bind_audit(
    client, async_session, sms_mock, monkeypatch
):
    """The agent's free, unregistered number is bound to it as on any send,
    and the bind is audited with that number, not the trial number."""
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    number = await _number(async_session, org, registered=False)
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    async_session.add(agent)
    await async_session.flush()
    voice = await _number(
        async_session, org, registered=False, capabilities=("voice",), e164=VOICE_NUMBER
    )
    call = Call(
        organization_id=org,
        from_number_id=voice.id,
        from_e164=voice.e164,
        to_e164=PHONE,
        status="in_progress",
        agent_id=agent.id,
        voice_config={},
        metadata_={},
    )
    async_session.add(call)
    await async_session.commit()
    data = await _post(
        client,
        "/internal/agent/send-sms",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": "hi",
        },
    )

    assert data == SENT["in_call"]
    assert sms_mock.send_sms.await_args.kwargs["from_e164"] == TRIAL_E164
    await async_session.refresh(number)
    assert number.sms_agent_id == agent.id
    route = (
        await async_session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org, AuditLog.action == "number.route"
            )
        )
    ).scalar_one()
    assert route.resource_id == number.id
    assert route.payload["e164"] == OWN_NUMBER


async def test_in_call_unregistered_number_refused_keeps_todays_block(
    client, async_session, sms_mock, monkeypatch
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    await _number(async_session, org, registered=False)
    send = await _in_call(async_session, org, OTHER_PHONE, registered=False)

    assert await send(client) == {"ok": False, "spoken": _SPOKEN_NOT_ALLOWED}
    sms_mock.send_sms.assert_not_awaited()
    audit = (
        await async_session.execute(
            select(AuditLog).where(
                AuditLog.organization_id == org,
                AuditLog.action == "agent.sms.blocked",
            )
        )
    ).scalar_one()
    assert audit.payload["reason"] == BLOCK_MESSAGE


async def test_handover_long_notice_keeps_footer(
    client, async_session, sms_mock, monkeypatch
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    send = await _handover(
        async_session, org, PHONE, registered=False, agent_name="A" * 450
    )

    assert await send(client) == {"sms": "sent"}
    [row] = await _outbound(async_session, org)
    assert len(row.body) == SMS_MAX_BODY_CHARS
    assert row.body.startswith(TRIAL_PREFIX + "A" * 10)
    assert row.body.endswith(TRIAL_FOOTER)


async def test_handover_voice_only_number_texts_the_verified_phone(
    client, async_session, sms_mock, monkeypatch
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    send = await _handover(
        async_session, org, PHONE, registered=False, capabilities=("voice",)
    )

    assert await send(client) == {"sms": "sent"}
    assert sms_mock.send_sms.await_args.kwargs["from_e164"] == TRIAL_E164


@pytest.mark.parametrize("case", ["not_verified_phone", "cap_used_25", "feature_off"])
async def test_handover_voice_only_number_still_skips_other_contacts(
    client, async_session, sms_mock, monkeypatch, case
):
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    async_session.add(Organization(id=org, origin="agent"))
    await async_session.commit()
    await verify_trial_phone(async_session, org, PHONE)
    to = OTHER_PHONE if case == "not_verified_phone" else PHONE
    if case == "cap_used_25":
        await seed_trial_texts(async_session, org, 25)
    if case == "feature_off":
        await set_sms_trial(async_session, ready=False)
    send = await _handover(
        async_session, org, to, registered=False, capabilities=("voice",)
    )

    assert await send(client) == {"sms": "skipped:number_cannot_text"}
    sms_mock.send_sms.assert_not_awaited()
    if case != "cap_used_25":
        # Returned before the gates: the send caps logged nothing.
        logs = (
            (
                await async_session.execute(
                    select(AgentSendLog).where(AgentSendLog.organization_id == org)
                )
            )
            .scalars()
            .all()
        )
        assert logs == []


async def test_browser_demo_call_never_uses_trial(
    client, async_session, sms_mock, monkeypatch
):
    """The hail.so demo keeps its own sender rules."""
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    send = await _browser(async_session, org, PHONE, registered=False, demo=True)

    assert await send(client) == {"ok": False, "spoken": _SPOKEN_NOT_ALLOWED}
    sms_mock.send_sms.assert_not_awaited()


async def test_reply_sms_never_uses_trial(client, async_session, sms_mock, monkeypatch):
    """Replies to an inbound text stay on the number the person wrote to."""
    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    async_session.add(_credit(org))
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Help.")
    async_session.add(agent)
    await async_session.flush()
    async_session.add(
        PhoneNumber(
            organization_id=org,
            e164=OWN_NUMBER,
            country_code="US",
            number_type="local",
            provisioning_state="active",
            provider_resource_id="PN_own",
            sms_agent_id=agent.id,
        )
    )
    await async_session.commit()
    inbound = await ingest_inbound_sms(
        async_session,
        from_e164=PHONE,
        to_e164=OWN_NUMBER,
        body="Can I book?",
        provider_message_sid="SM-in-trial",
        opt_out_type=None,
        carrier="twilio",
    )
    await async_session.commit()

    data = await _post(
        client,
        "/internal/agent/reply-sms",
        {"sms_id": str(inbound.sms_id), "body": "Yes."},
    )
    assert data["ok"] is False and data["state"] == "skipped"
    assert data["reason"] == "sms_registration"
    sms_mock.send_sms.assert_not_awaited()


@pytest.mark.parametrize("case", ["not_verified_phone", "feature_off"])
async def test_handover_blocked_notice_skips_trial_work_for_other_contacts(
    client, async_session, sms_mock, monkeypatch, case
):
    """A US registration block on a contact that is not the verified trial
    phone answers before the notice text is built or a trial is claimed."""
    from hailhq.api.routes.internal import agent as agent_routes

    await seed_sms_trial(async_session)
    org = uuid.uuid4()
    await verify_trial_phone(async_session, org, PHONE)
    to = OTHER_PHONE if case == "not_verified_phone" else PHONE
    if case == "feature_off":
        await set_sms_trial(async_session, ready=False)
    calls: list[str] = []
    real_claim = agent_routes.claim_trial_send
    real_text = agent_routes._notice_text

    async def claim_spy(*args, **kwargs):
        calls.append("claim")
        return await real_claim(*args, **kwargs)

    async def text_spy(*args, **kwargs):
        calls.append("text")
        return await real_text(*args, **kwargs)

    monkeypatch.setattr(agent_routes, "claim_trial_send", claim_spy)
    monkeypatch.setattr(agent_routes, "_notice_text", text_spy)
    send = await _handover(async_session, org, to, registered=False)

    assert await send(client) == {"sms": "blocked:us_registration"}
    assert calls == []
    sms_mock.send_sms.assert_not_awaited()
