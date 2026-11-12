"""Email-run files use the claimed, authenticated sender's thread."""

import asyncio
import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ConnectTimeoutError
from hailhq.api import deps
from hailhq.api.routes import emails
from hailhq.core.models import Email, EmailAttachment
from sqlalchemy import select, update

from tests.test_internal_agent_reply_email import (
    _internal_secret,  # noqa: F401 - provider fixture
    _reply,
    _seed,
    _signed,
)


async def post(client, path, data):
    raw = json.dumps(data).encode()
    return await client.post(path, content=raw, headers=_signed(raw))


async def file(db, email):
    att = EmailAttachment(
        email_id=email.id,
        filename="receipt.png",
        content_type="image/png",
        size_bytes=7,
        s3_key=f"attachments/{email.id}/receipt",
    )
    db.add(att)
    await db.commit()
    return att


@pytest.mark.asyncio
async def test_claimed_email_reads_original_attachment(
    client, async_session, monkeypatch
):
    _, _, _, inbound = await _seed(async_session)
    att = await file(async_session, inbound)
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    result = await post(
        client,
        "/internal/agent/email-run/inbox",
        {
            "inbound_email_id": str(inbound.id),
            "attachment_id": str(att.id),
            "attachment_format": "inspection_v1",
        },
    )
    assert result.status_code == 200, result.text
    assert base64.b64decode(result.json()["attachment_data"]["base64"]) == b"pngdata"


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["organization", "sender", "thread"])
@pytest.mark.parametrize("action", ["read", "reply"])
async def test_email_file_reads_do_not_follow_forged_references(
    client, async_session, monkeypatch, foreign, action, email_mock
):
    org, _, domain, inbound = await _seed(async_session)
    other = Email(
        organization_id=uuid.uuid4() if foreign == "organization" else org,
        email_domain_id=domain.id,
        direction="inbound",
        from_address=(
            "other@example.test" if foreign == "sender" else inbound.from_address
        ),
        to_addresses=inbound.to_addresses,
        subject="Secret",
        body_text="Secret",
        status="received",
        from_verified=True,
        message_id="<secret@example.test>",
    )
    async_session.add(other)
    await async_session.flush()
    att = await file(async_session, other)
    if foreign != "thread":
        inbound.references_ids = [other.message_id]
        await async_session.commit()
    s3 = AsyncMock()
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    if action == "read":
        result = await post(
            client,
            "/internal/agent/email-run/inbox",
            {
                "inbound_email_id": str(inbound.id),
                "attachment_id": str(att.id),
                "attachment_format": "inspection_v1",
            },
        )
        assert result.status_code == 200, result.text
        assert (
            "attachment_data" not in result.json() and "receipt.png" not in result.text
        )
    else:
        result = await post(
            client,
            "/internal/agent/email-run/send-email",
            {
                "inbound_email_id": str(inbound.id),
                "tool_invocation_id": str(uuid.uuid4()),
                "recipient_name": "sender",
                "subject": "File",
                "body_text": "File",
                "reply_to_email_id": str(inbound.id),
                "attachment_ids": [str(att.id)],
            },
        )
        assert result.status_code == 200 and result.json()["ok"] is False, result.text
        email_mock.send_email.assert_not_awaited()
    s3.fetch_raw.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("outside", [False, True])
async def test_email_attachment_reply_is_the_final_reply_not_a_second_email(
    client, async_session, monkeypatch, email_mock, outside
):
    _, _, _, inbound = await _seed(
        async_session,
        **({"scope": "anyone", "sender": "outside@example.test"} if outside else {}),
    )
    att = await file(async_session, inbound)
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    result = await post(
        client,
        "/internal/agent/email-run/send-email",
        {
            "inbound_email_id": str(inbound.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "sender",
            "subject": "Your file",
            "body_text": "Here is your original file.",
            "reply_to_email_id": str(inbound.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert result.status_code == 200 and result.json()["ok"], result.text
    assert (
        email_mock.send_email.call_args.kwargs["attachments"][0].payload == b"pngdata"
    )
    assert (
        email_mock.send_email.call_args.kwargs["headers"]["Auto-Submitted"]
        == "auto-replied"
    )
    assert (await _reply(client, inbound.id))["state"] == "done"
    assert email_mock.send_email.await_count == 1
    outbound = (
        await async_session.execute(select(Email).where(Email.direction == "outbound"))
    ).scalar_one()
    assert outbound.to_addresses == [inbound.from_address]


@pytest.mark.asyncio
@pytest.mark.parametrize("older_thread", [False, True])
async def test_safe_email_retry_keeps_original_attachments(
    client, async_session, monkeypatch, email_mock, older_thread
):
    _, _, _, inbound = await _seed(async_session)
    source = inbound
    if older_thread:
        source = Email(
            organization_id=inbound.organization_id,
            email_domain_id=inbound.email_domain_id,
            direction="inbound",
            from_address=inbound.from_address,
            from_verified=True,
            to_addresses=inbound.to_addresses,
            status="received",
            subject="Older receipt",
            body_text="Receipt from a separate thread",
            message_id="<older@example.test>",
        )
        async_session.add(source)
        await async_session.commit()
    att = await file(async_session, source)
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    original_send = email_mock.send_email.side_effect
    email_mock.send_email.side_effect = ConnectTimeoutError(
        endpoint_url="https://email.example.test"
    )
    result = await post(
        client,
        "/internal/agent/email-run/send-email",
        {
            "inbound_email_id": str(inbound.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "sender",
            "subject": "Your file",
            "body_text": "Your file.",
            "attachment_ids": [str(att.id)],
            "reply_to_email_id": str(source.id),
        },
    )
    assert result.status_code == 200 and result.json()["ok"] is False, result.text
    email_mock.send_email.side_effect = original_send
    result = await _reply(client, inbound.id)
    assert result["state"] == "done", result
    assert (
        email_mock.send_email.call_args.kwargs["headers"]["In-Reply-To"]
        == inbound.message_id
    )
    assert (
        email_mock.send_email.call_args.kwargs["attachments"][0].payload == b"pngdata"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["unverified", "ended", "quota"])
async def test_email_file_access_closes_with_claim_and_budget(
    client, async_session, monkeypatch, reason
):
    _, _, _, inbound = await _seed(async_session)
    att = await file(async_session, inbound)
    if reason == "unverified":
        inbound.from_verified = False
    elif reason == "ended":
        inbound.agent_reply_state = "done"
    else:
        inbound.metadata_ = {"attachment_reads": 8}
    await async_session.commit()
    s3 = AsyncMock()
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    result = await post(
        client,
        "/internal/agent/email-run/inbox",
        {
            "inbound_email_id": str(inbound.id),
            "attachment_id": str(att.id),
            "attachment_format": "inspection_v1",
        },
    )
    assert "attachment_data" not in result.json()
    s3.fetch_raw.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_claim", [True, False])
@pytest.mark.parametrize(
    "change", ["expired_fetch", "expired_upload", "superseded", "settled"]
)
async def test_attachment_preparation_cannot_send_after_its_claim_ends(
    async_session, monkeypatch, email_mock, change, explicit_claim
):
    _, agent, domain, inbound = await _seed(async_session)
    att = await file(async_session, inbound)
    reply = Email(
        organization_id=inbound.organization_id,
        email_domain_id=domain.id,
        agent_id=agent.id,
        direction="outbound",
        from_address=domain.domain,
        to_addresses=[inbound.from_address],
        subject="Your receipt",
        body_text="Here is your file.",
        status="queued",
        requested_at=datetime.now(timezone.utc),
    )
    async_session.add(reply)
    await async_session.commit()
    claimed_at = reply.requested_at
    s3 = AsyncMock()

    async def fetch(*args, **kwargs):
        if change == "expired_fetch":
            # A short budget lets real cancellation stop a stalled S3 read.
            await asyncio.sleep(0.3)
        elif change in {"superseded", "settled"}:
            values = (
                {"requested_at": claimed_at + timedelta(seconds=1)}
                if change == "superseded"
                else {"status": "failed", "end_reason": "DeliveryUnknown"}
            )
            await async_session.execute(
                update(Email)
                .where(Email.id == reply.id)
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            await async_session.commit()
        return b"pngdata"

    s3.fetch_raw.side_effect = fetch
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    if change.startswith("expired"):
        monkeypatch.setattr(emails, "SES_SEND_BUDGET_SECONDS", 0.1)

    async def stalled_upload(*args, **kwargs):
        await asyncio.sleep(0.3)

    if change == "expired_upload":
        s3.put_attachment.side_effect = stalled_upload
    error = await emails.deliver_email(
        async_session,
        email_mock,
        reply,
        [att],
        claimed_at=claimed_at if explicit_claim else None,
        at_most_once=True,
    )
    assert error == (
        "TimeoutError" if change.startswith("expired") else "SendClaimLost"
    )
    email_mock.send_email.assert_not_awaited()
    await async_session.refresh(reply)
    if change == "superseded":
        assert reply.status == "queued" and reply.requested_at != claimed_at
    else:
        assert reply.status == "failed"
        assert reply.end_reason == (
            "TimeoutError" if change.startswith("expired") else "DeliveryUnknown"
        )


@pytest.mark.asyncio
async def test_provider_timeout_after_acceptance_is_never_resent(
    client, async_session, monkeypatch, email_mock
):
    _, _, _, inbound = await _seed(async_session)
    accepted = []

    async def accepted_then_stalled(**kwargs):
        accepted.append(kwargs["body_text"])
        await asyncio.sleep(3)

    email_mock.send_email.side_effect = accepted_then_stalled
    monkeypatch.setattr(emails, "SES_SEND_BUDGET_SECONDS", 1)
    first = await _reply(client, inbound.id)
    assert first["state"] == "failed" and first["reason"] == "TimeoutError"
    again = await _reply(client, inbound.id)
    assert again["state"] == "failed" and again["reason"] == "TimeoutError"
    assert accepted == ["Happy to help."]
    outbound = (
        await async_session.execute(select(Email).where(Email.direction == "outbound"))
    ).scalar_one()
    assert outbound.status == "failed" and outbound.end_reason == "TimeoutError"
