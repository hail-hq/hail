"""Real browser sends stay contact-scoped, bounded, and retry-safe."""

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from hailhq.core.config import settings
from hailhq.core.hmac_signing import sign
from hailhq.core.models import (
    Call,
    Email,
    EmailAttachment,
    EmailDomain,
    OrganizationMember,
    Sms,
    UsageEvent,
    User,
)
from sqlalchemy import select

SECRET = "browser-messaging-test"


async def post(client, path, body):
    raw = json.dumps(body).encode()
    return await client.post(
        path,
        content=raw,
        headers={
            "content-type": "application/json",
            "x-hail-signature": sign(raw, SECRET),
        },
    )


@pytest.fixture(autouse=True)
def config(monkeypatch):
    # Preview deliberately empties profile vars; boto3 treats an empty profile
    # as an explicit (invalid) profile. Tests supply their own fake keys.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)
    monkeypatch.setattr(settings, "hail_browser_messaging_enabled", True)
    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr(settings, "hail_mail_name_prefix", "browser-test")
    monkeypatch.setattr(settings, "hail_inbound_enabled", True)
    monkeypatch.setattr(settings, "hail_inbound_hmac_secret", "test-inbound")
    monkeypatch.setattr(settings, "aws_region", "us-east-1")
    monkeypatch.setattr(settings, "aws_access_key_id", "test-key")
    monkeypatch.setattr(settings, "aws_secret_access_key", "test-secret")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "test-secret")


async def seed(db):
    org, uid = uuid.uuid4(), uuid.uuid4()
    now = datetime.now(timezone.utc)
    db.add(
        User(
            id=uid,
            name="Demo Owner",
            email="owner@example.test",
            email_verified=True,
            created_at=now,
        )
    )
    db.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=uid,
            organization_id=org,
            role="owner",
            created_at=now,
        )
    )
    domain = EmailDomain(
        organization_id=org,
        kind="hail_mail",
        domain="demo+workspace@mail.example.test",
        local_prefix_user="demo",
        local_prefix_org="workspace",
        verification_status="verified",
        dns_records=[],
    )
    db.add(domain)
    await db.flush()
    call = Call(
        organization_id=org,
        transport="browser",
        provider="browser",
        direction="inbound",
        status="in_progress",
        max_duration_seconds=300,
        voice_config={},
        metadata_={
            "browser_trial": True,
            "browser_contacts": {"email": "owner@example.test", "sms": "+46705550123"},
            "browser_capabilities": {
                "inboxAddress": domain.domain,
                "inboundEmailReady": True,
            },
        },
    )
    db.add(call)
    await db.commit()
    return call, domain


@pytest.mark.asyncio
async def test_email_real_send_fixed_contact_quota_retry_and_no_trial_charge(
    client, async_session, email_mock
):
    call, _domain = await seed(async_session)
    bodies = []
    for n in range(4):
        body = {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "attacker@elsewhere.test",
            "subject": "Test",
            "body_text": "A real test message.",
        }
        bodies.append(body)
        response = await post(client, "/internal/agent/send-email", body)
        assert response.status_code == 200, response.text
        assert response.json()["ok"] is (n < 3)
    call.status = "completed"
    call.end_reason = "normal_hangup"
    await async_session.commit()
    retry = await post(client, "/internal/agent/send-email", bodies[0])
    assert retry.json()["ok"] is True
    rows = (await async_session.execute(select(Email))).scalars().all()
    assert len(rows) == 3
    assert all(
        row.to_addresses == ["owner@example.test"] and row.status == "sent"
        for row in rows
    )
    assert email_mock.send_email.call_count == 3
    assert (await async_session.execute(select(UsageEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_sms_eligible_pool_keeps_owner_and_inbound_routing_unchanged(
    client, async_session, sms_mock, add_phone_number
):
    call, _ = await seed(async_session)
    pool = await add_phone_number(
        async_session, None, e164="+14155550111", is_pool=True
    )
    body = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": "Test text",
    }
    response = await post(client, "/internal/agent/send-sms", body)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert (await post(client, "/internal/agent/send-sms", body)).json()["ok"] is True
    rows = (await async_session.execute(select(Sms))).scalars().all()
    assert len(rows) == 1 and rows[0].to_e164 == "+46705550123"
    assert (
        rows[0].from_number_id == pool.id
        and rows[0].organization_id == call.organization_id
    )
    await async_session.refresh(pool)
    assert pool.organization_id is None and pool.sms_agent_id is None
    assert sms_mock.send_sms.call_count == 1
    assert (await async_session.execute(select(UsageEvent))).scalars().all() == []


@pytest.mark.asyncio
async def test_pool_cannot_bypass_us_registration_or_missing_contact(
    client, async_session, add_phone_number, sms_mock
):
    call, _ = await seed(async_session)
    await add_phone_number(async_session, None, e164="+14155550111", is_pool=True)
    call.metadata_ = {**call.metadata_, "browser_contacts": {"sms": "+14155550123"}}
    await async_session.commit()
    body = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "body": "Test text",
    }
    assert (await post(client, "/internal/agent/send-sms", body)).json()["ok"] is False
    call.metadata_ = {**call.metadata_, "browser_contacts": {}}
    await async_session.commit()
    assert (await post(client, "/internal/agent/send-sms", body)).json()["ok"] is False
    sms_mock.send_sms.assert_not_called()


@pytest.mark.asyncio
async def test_capabilities_require_verified_member_email_and_explicit_sms_consent(
    client, async_session, add_phone_number
):
    call, _domain = await seed(async_session)
    await add_phone_number(async_session, None, e164="+14155550111", is_pool=True)
    body = {
        "organization_id": str(call.organization_id),
        "email_recipient": "owner@example.test",
        "sms_recipient": "+46705550123",
    }
    first = (await post(client, "/internal/browser-sessions/capabilities", body)).json()
    assert (
        first["emailSendReady"]
        and first["inboundEmailReady"]
        and not first["smsSendReady"]
    )
    allowed = (
        await post(
            client,
            "/internal/browser-sessions/capabilities",
            {**body, "recipient_consent": True},
        )
    ).json()
    assert allowed["smsSendReady"] and not allowed["smsReceiveReady"]
    denied = (
        await post(
            client,
            "/internal/browser-sessions/capabilities",
            {**body, "email_recipient": "stranger@example.test"},
        )
    ).json()
    assert not denied["emailSendReady"] and denied["emailRecipient"] is None


@pytest.mark.asyncio
async def test_inbox_and_attachment_contents_are_scoped_and_untrusted(
    client, async_session, monkeypatch
):
    call, domain = await seed(async_session)
    own = Email(
        organization_id=call.organization_id,
        email_domain_id=domain.id,
        direction="inbound",
        from_address="owner@example.test",
        to_addresses=[domain.domain],
        subject="Notes",
        body_text="Read my notes",
        status="received",
    )
    other = Email(
        organization_id=uuid.uuid4(),
        direction="inbound",
        from_address="private@example.test",
        to_addresses=[domain.domain],
        subject="Private",
        body_text="Secret",
        status="received",
    )
    async_session.add_all([own, other])
    await async_session.flush()
    attachment = EmailAttachment(
        email_id=own.id,
        filename="notes.txt",
        content_type="text/plain",
        size_bytes=15,
        s3_key="private-key",
    )
    foreign = EmailAttachment(
        email_id=other.id,
        filename="secret.txt",
        content_type="text/plain",
        size_bytes=6,
        s3_key="other-key",
    )
    async_session.add_all([attachment, foreign])
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"Call on Monday."
    monkeypatch.setattr("hailhq.api.deps.get_s3_mail_optional", lambda: s3)
    path = "/internal/browser-sessions/inbox"
    listing = (await post(client, path, {"call_id": str(call.id)})).json()
    assert len(listing["quoted_untrusted_messages"]) == 1
    content = (
        await post(
            client, path, {"call_id": str(call.id), "attachment_id": str(attachment.id)}
        )
    ).json()
    assert content["quoted_untrusted_content"] == "Call on Monday."
    assert "private-key" not in json.dumps(listing) + json.dumps(content)
    denied = (
        await post(
            client, path, {"call_id": str(call.id), "attachment_id": str(foreign.id)}
        )
    ).json()
    assert "cannot find" in denied["spoken"]
    s3.fetch_raw.assert_awaited_once_with("private-key")


@pytest.mark.asyncio
async def test_sms_single_segment_cap_and_independent_lifetime_budget(
    client, async_session, add_phone_number, sms_mock
):
    call, _ = await seed(async_session)
    await add_phone_number(async_session, None, e164="+14155550111", is_pool=True)
    path = "/internal/agent/send-sms"

    def payload(text):
        return {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "body": text,
        }

    assert not (await post(client, path, payload("🙂" * 36))).json()["ok"]
    for _ in range(3):
        assert (await post(client, path, payload("A short text"))).json()["ok"]
    assert not (await post(client, path, payload("One more"))).json()["ok"]
    # Email has an independent budget, and a later call shares SMS lifetime use.
    email_body = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "recipient_name": "you",
        "subject": "Test",
        "body_text": "Email after texts.",
    }
    assert (await post(client, "/internal/agent/send-email", email_body)).json()["ok"]
    second = Call(
        organization_id=call.organization_id,
        transport="browser",
        provider="browser",
        direction="inbound",
        status="in_progress",
        max_duration_seconds=300,
        voice_config={},
        metadata_={"browser_trial": True, "browser_contacts": {"sms": "+46705550123"}},
    )
    async_session.add(second)
    await async_session.commit()
    assert not (
        await post(client, path, {**payload("Later"), "call_id": str(second.id)})
    ).json()["ok"]
    assert sms_mock.send_sms.call_count == 3


@pytest.mark.asyncio
async def test_parallel_browser_sends_cannot_exceed_workspace_quota(
    client, async_session, session_factory, email_mock
):
    import asyncio

    from hailhq.api.main import app
    from hailhq.core.db import get_session

    call, _ = await seed(async_session)

    async def per_request():
        async with session_factory() as db:
            yield db

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request
    try:
        results = await asyncio.gather(
            *[
                post(
                    client,
                    "/internal/agent/send-email",
                    {
                        "call_id": str(call.id),
                        "tool_invocation_id": str(uuid.uuid4()),
                        "recipient_name": "you",
                        "subject": "Test",
                        "body_text": "Concurrent message",
                    },
                )
                for _ in range(5)
            ]
        )
    finally:
        app.dependency_overrides[get_session] = saved
    assert sum(r.json()["ok"] for r in results) == 3
    assert email_mock.send_email.call_count == 3
    await async_session.refresh(call)
    assert len(call.metadata_["browser_actions"]) == 3
    assert all(a["status"] == "sent" for a in call.metadata_["browser_actions"])


@pytest.mark.asyncio
async def test_disabled_providers_do_not_claim_readiness_and_inbox_survives_unverified_contact(
    client, async_session, monkeypatch
):
    call, domain = await seed(async_session)
    monkeypatch.setattr(settings, "hail_browser_messaging_enabled", False)
    monkeypatch.setattr(settings, "twilio_auth_token", "")
    result = (
        await post(
            client,
            "/internal/browser-sessions/capabilities",
            {
                "organization_id": str(call.organization_id),
                "email_recipient": "unverified@example.test",
                "sms_recipient": "+46705550123",
                "recipient_consent": True,
            },
        )
    ).json()
    assert result["inboxAddress"] == domain.domain
    assert (
        result["inboundEmailReady"]
        and not result["emailSendReady"]
        and not result["smsSendReady"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_failed_or_cancelled_external_send_retry_never_resends(
    client, async_session, email_mock, cancel
):
    import asyncio

    call, _ = await seed(async_session)
    started = asyncio.Event()

    async def external(**kwargs):
        started.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("transport lost")

    email_mock.send_email.side_effect = external
    body = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "recipient_name": "you",
        "subject": "Test",
        "body_text": "One attempt",
    }
    path = "/internal/agent/send-email"
    task = asyncio.create_task(post(client, path, body))
    await asyncio.wait_for(started.wait(), timeout=3)
    if cancel:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        assert not (await task).json()["ok"]
    replay = (await post(client, path, body)).json()
    assert replay["ok"] is cancel
    if cancel:
        assert "still being submitted" in replay["spoken"]
    assert email_mock.send_email.await_count == 1
    await async_session.refresh(call)
    assert len(call.metadata_["browser_actions"]) == 1


@pytest.mark.asyncio
async def test_browser_email_readiness_uses_resolved_default_aws_credentials(
    monkeypatch,
):
    from types import SimpleNamespace

    from hailhq.api.browser_messaging import email_provider_ready
    from hailhq.core.providers.email import ses

    monkeypatch.setattr(settings, "aws_access_key_id", "")
    monkeypatch.setattr(settings, "aws_secret_access_key", "")
    for key in (
        "AWS_PROFILE",
        "AWS_ROLE_ARN",
        "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
        "AWS_EXECUTION_ENV",
    ):
        monkeypatch.delenv(key, raising=False)
    credentials = SimpleNamespace(
        get_frozen_credentials=lambda: SimpleNamespace(
            access_key="role-key", secret_key="role-secret"
        )
    )
    monkeypatch.setattr(
        ses.boto3,
        "Session",
        lambda **kwargs: SimpleNamespace(
            region_name="us-east-1", get_credentials=lambda: credentials
        ),
    )
    assert await email_provider_ready()
    monkeypatch.setattr(
        ses.boto3,
        "Session",
        lambda **kwargs: SimpleNamespace(
            region_name="us-east-1", get_credentials=lambda: None
        ),
    )
    assert not await email_provider_ready()


@pytest.mark.asyncio
async def test_telnyx_missing_webhook_key_falls_back_to_configured_sms_sender(
    async_session, add_phone_number, monkeypatch
):
    from hailhq.api.browser_messaging import sms_sender

    call, _ = await seed(async_session)
    monkeypatch.setattr(settings, "telnyx_api_key", "test-key")
    monkeypatch.setattr(settings, "telnyx_public_key", "")
    first = await add_phone_number(
        async_session, None, e164="+14155550112", is_pool=True, provider="telnyx"
    )
    fallback = await add_phone_number(
        async_session, None, e164="+14155550113", is_pool=True
    )
    assert (
        await sms_sender(async_session, call.organization_id, "+46705550123")
    ).id == fallback.id
    monkeypatch.setattr(settings, "telnyx_public_key", "test-public-key")
    assert (
        await sms_sender(async_session, call.organization_id, "+46705550123")
    ).id == first.id


@pytest.mark.asyncio
async def test_email_readiness_reads_shared_credentials_without_profile_hint(
    monkeypatch, tmp_path
):
    from hailhq.api.browser_messaging import email_provider_ready

    credentials = tmp_path / "credentials"
    credentials.write_text(
        "[default]\naws_access_key_id = fixture-key\naws_secret_access_key = fixture-secret\n"
    )
    config_file = tmp_path / "config"
    config_file.write_text("[default]\nregion = us-east-1\n")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(config_file))
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    for key in (
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
    ):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(settings, "aws_region", "")
    monkeypatch.setattr(settings, "aws_access_key_id", "")
    monkeypatch.setattr(settings, "aws_secret_access_key", "")
    assert await email_provider_ready()


@pytest.mark.asyncio
async def test_disabled_browser_messaging_never_resolves_credentials(monkeypatch):
    from hailhq.api import browser_messaging

    resolve = AsyncMock(side_effect=AssertionError("Credential chain must stay unused"))
    monkeypatch.setattr(browser_messaging, "credentials_ready", resolve)
    monkeypatch.setattr(settings, "hail_browser_messaging_enabled", False)
    assert not await browser_messaging.email_provider_ready()
    assert not browser_messaging.sms_provider_ready("twilio")
    resolve.assert_not_called()
