"""File reads/reuse follow the authenticated person's mail thread."""

import base64
import uuid
from unittest.mock import AsyncMock

import pytest
from hailhq.api import deps
from hailhq.api.routes import emails
from hailhq.core.models import (
    AccountCredit,
    Email,
    EmailAttachment,
    EmailDomain,
    FileUpload,
    PhoneNumber,
    User,
)
from sqlalchemy import select

from tests.test_browser_messaging import (
    config,  # noqa: F401 - reuse the provider fixture
    post,
    seed,
)


async def mail(
    db,
    call,
    domain,
    *,
    sender="owner@example.test",
    org=None,
    message_id="<source@example.test>",
):
    row = Email(
        organization_id=org or call.organization_id,
        email_domain_id=domain.id if org is None else None,
        direction="inbound",
        from_address=sender,
        from_verified=True,
        to_addresses=[domain.domain],
        subject="Receipt",
        body_text="Here is my receipt",
        status="received",
        message_id=message_id,
    )
    db.add(row)
    await db.flush()
    att = EmailAttachment(
        email_id=row.id,
        filename="receipt.png",
        content_type="image/png",
        size_bytes=7,
        s3_key=f"attachments/{row.id}/original",
    )
    db.add(att)
    await db.commit()
    return row, att


@pytest.mark.asyncio
async def test_inbox_does_not_read_another_person_in_the_same_workspace(
    client, async_session
):
    call, domain = await seed(async_session)
    _row, att = await mail(
        async_session, call, domain, sender="someone-else@example.test"
    )
    response = await post(
        client, "/internal/browser-sessions/inbox", {"call_id": str(call.id)}
    )
    assert response.json()["quoted_untrusted_messages"] == []
    response = await post(
        client,
        "/internal/browser-sessions/inbox",
        {
            "call_id": str(call.id),
            "attachment_id": str(att.id),
            "attachment_format": "inspection_v1",
        },
    )
    assert "attachment_data" not in response.json()
    assert "receipt.png" not in response.text


@pytest.mark.asyncio
async def test_image_read_returns_private_bytes_only_after_user_and_org_checks(
    client, async_session, monkeypatch
):
    call, domain = await seed(async_session)
    _, att = await mail(async_session, call, domain)
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    response = await post(
        client,
        "/internal/browser-sessions/inbox",
        {
            "call_id": str(call.id),
            "attachment_id": str(att.id),
            "attachment_format": "inspection_v1",
        },
    )
    result = response.json()
    assert base64.b64decode(result["attachment_data"]["base64"]) == b"pngdata"
    assert "s3_key" not in response.text and att.s3_key not in response.text


@pytest.mark.asyncio
async def test_reattaches_original_bytes_and_keeps_reply_headers(
    client, async_session, monkeypatch, email_mock
):
    call, domain = await seed(async_session)
    source, att = await mail(async_session, call, domain)
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    body = {
        "call_id": str(call.id),
        "tool_invocation_id": str(uuid.uuid4()),
        "recipient_name": "you",
        "subject": "Your receipt",
        "body_text": "Here is the original.",
        "reply_to_email_id": str(source.id),
        "attachment_ids": [str(att.id)],
    }
    response = await post(client, "/internal/agent/send-email", body)
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True, response.text
    sent = email_mock.send_email.call_args.kwargs
    assert sent["attachments"][0].payload == b"pngdata"
    assert sent["headers"]["In-Reply-To"] == source.message_id
    assert source.message_id in sent["headers"]["References"]
    result = (
        await async_session.execute(select(Email).where(Email.direction == "outbound"))
    ).scalar_one()
    copied = (
        await async_session.execute(
            select(EmailAttachment).where(EmailAttachment.email_id == result.id)
        )
    ).scalar_one()
    assert (
        copied.filename == att.filename
        and copied.id != att.id
        and copied.s3_key != att.s3_key
        and copied.s3_key.startswith(f"outbound-attachments/{call.organization_id}/")
    )
    s3.put_attachment.assert_awaited_once_with(
        copied.s3_key, b"pngdata", att.content_type
    )
    retained = (
        await async_session.execute(
            select(FileUpload).where(FileUpload.s3_key == copied.s3_key)
        )
    ).scalar_one()
    assert retained.first_used_at is not None
    assert (await post(client, "/internal/agent/send-email", body)).json()["ok"] is True
    assert email_mock.send_email.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign", ["organization", "person", "thread"])
async def test_reuse_rejects_foreign_files_before_fetch_or_send(
    client, async_session, monkeypatch, email_mock, foreign
):
    call, domain = await seed(async_session)
    source, _ = await mail(async_session, call, domain)
    _, att = await mail(
        async_session,
        call,
        domain,
        org=uuid.uuid4() if foreign == "organization" else None,
        sender=(
            "elsewhere@example.test" if foreign == "person" else "owner@example.test"
        ),
        message_id="<other-thread@example.test>",
    )
    s3 = AsyncMock()
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    response = await post(
        client,
        "/internal/agent/send-email",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "you",
            "subject": "Files",
            "body_text": "Files",
            "reply_to_email_id": str(source.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["ok"] is False
    s3.fetch_raw.assert_not_awaited()
    email_mock.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_reuses_a_file_from_an_earlier_email_in_the_same_thread(
    client, async_session, monkeypatch, email_mock
):
    call, domain = await seed(async_session)
    old, att = await mail(async_session, call, domain)
    reply, _ = await mail(
        async_session, call, domain, message_id="<reply@example.test>"
    )
    reply.references_ids = [old.message_id]
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    result = await post(
        client,
        "/internal/agent/send-email",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "you",
            "subject": "Original",
            "body_text": "Your original file.",
            "reply_to_email_id": str(reply.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert result.json()["ok"] is True, result.text
    assert (
        email_mock.send_email.call_args.kwargs["attachments"][0].payload == b"pngdata"
    )


@pytest.mark.asyncio
async def test_returns_an_original_outbound_file_after_a_reply_to_its_ses_id(
    client, async_session, monkeypatch, email_mock
):
    call, domain = await seed(async_session)
    original, att = await mail(async_session, call, domain)
    original.direction = "outbound"
    original.from_address = domain.domain
    original.to_addresses = ["owner@example.test"]
    original.message_id = None
    original.provider_message_id = "ses-original"
    reply, _ = await mail(
        async_session, call, domain, message_id="<reply@example.test>"
    )
    reply.in_reply_to = "<ses-original@email.amazonses.com>"
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    result = await post(
        client,
        "/internal/agent/send-email",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "you",
            "subject": "Original",
            "body_text": "Your original file.",
            "reply_to_email_id": str(reply.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert result.status_code == 200, result.text
    assert result.json()["ok"] is True, result.text
    sent = email_mock.send_email.call_args.kwargs
    assert sent["attachments"][0].payload == b"pngdata"
    assert sent["headers"]["In-Reply-To"] == reply.message_id


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["quota", "ended", "virus", "size"])
async def test_file_reads_stop_before_storage_when_not_allowed(
    client, async_session, monkeypatch, reason
):
    call, domain = await seed(async_session)
    email, att = await mail(async_session, call, domain)
    if reason == "quota":
        call.metadata_ = {**call.metadata_, "attachment_reads": 8}
    elif reason == "ended":
        call.status = "completed"
        call.end_reason = "normal_hangup"
    elif reason == "virus":
        email.virus_verdict = "FAIL"
    else:
        att.size_bytes = 10 * 1024 * 1024 + 1
    await async_session.commit()
    s3 = AsyncMock()
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    result = await post(
        client,
        "/internal/browser-sessions/inbox",
        {
            "call_id": str(call.id),
            "attachment_id": str(att.id),
            "attachment_format": "inspection_v1",
        },
    )
    assert "attachment_data" not in result.json()
    s3.fetch_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_forged_thread_references_do_not_expand_person_scope(
    client, async_session, email_mock
):
    call, domain = await seed(async_session)
    source, _ = await mail(async_session, call, domain)
    foreign, att = await mail(
        async_session,
        call,
        domain,
        sender="another@example.test",
        message_id="<foreign@example.test>",
    )
    foreign.references_ids = [source.message_id]
    await async_session.commit()
    result = await post(
        client,
        "/internal/agent/send-email",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "you",
            "subject": "Files",
            "body_text": "Files",
            "reply_to_email_id": str(source.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert result.json()["ok"] is False
    email_mock.send_email.assert_not_called()


@pytest.mark.asyncio
async def test_phone_caller_id_is_not_email_identity(client, async_session):
    call, domain = await seed(async_session)
    await mail(async_session, call, domain)
    number_id = (await async_session.execute(select(PhoneNumber.id))).scalar_one()
    call.transport = "phone"
    call.from_e164 = "+12125550123"
    call.to_e164 = "+12125550199"
    call.to_number_id = number_id
    await async_session.commit()
    result = await post(client, "/internal/agent/inbox", {"call_id": str(call.id)})
    assert "quoted_untrusted_messages" not in result.json()


@pytest.mark.asyncio
async def test_inbox_pages_to_older_mail_without_changing_scope(client, async_session):
    call, domain = await seed(async_session)
    for n in range(7):
        await mail(async_session, call, domain, message_id=f"<mail{n}@example.test>")
    first = (
        await post(
            client, "/internal/browser-sessions/inbox", {"call_id": str(call.id)}
        )
    ).json()["quoted_untrusted_messages"]
    older = (
        await post(
            client,
            "/internal/browser-sessions/inbox",
            {"call_id": str(call.id), "before_email_id": first[-1]["id"]},
        )
    ).json()["quoted_untrusted_messages"]
    assert len(first) == 5 and len(older) == 2
    assert not {m["id"] for m in first} & {m["id"] for m in older}


@pytest.mark.asyncio
async def test_failed_retained_copy_never_sends_and_remains_collectible(
    client, async_session, monkeypatch, email_mock
):
    call, domain = await seed(async_session)
    source, att = await mail(async_session, call, domain)
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    s3.put_attachment.side_effect = OSError("storage unavailable")
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    result = await post(
        client,
        "/internal/agent/send-email",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "you",
            "subject": "Original",
            "body_text": "Original",
            "reply_to_email_id": str(source.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert result.json()["ok"] is False
    email_mock.send_email.assert_not_called()
    upload = (await async_session.execute(select(FileUpload))).scalar_one()
    assert upload.first_used_at is None and upload.uploaded_at is not None


@pytest.mark.asyncio
async def test_older_worker_never_receives_binary_tool_data(
    client, async_session, monkeypatch
):
    call, domain = await seed(async_session)
    _, att = await mail(async_session, call, domain)
    s3 = AsyncMock()
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    result = await post(
        client,
        "/internal/browser-sessions/inbox",
        {"call_id": str(call.id), "attachment_id": str(att.id)},
    )
    assert "attachment_data" not in result.json() and "base64" not in result.text
    s3.fetch_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_verified_outbound_member_reads_the_same_custom_mailbox_used_for_sending(
    client, async_session
):
    call, _ = await seed(async_session)
    number = (await async_session.execute(select(PhoneNumber))).scalar_one()
    user = (await async_session.execute(select(User))).scalar_one()
    user.phone_number = "+12125550123"
    call.transport = "phone"
    call.direction = "outbound"
    call.from_number_id = number.id
    call.from_e164 = number.e164
    call.to_e164 = user.phone_number
    custom = EmailDomain(
        organization_id=call.organization_id,
        kind="custom",
        domain="team.example.test",
        verification_status="verified",
        dns_records=[],
    )
    async_session.add(custom)
    await async_session.flush()
    message = Email(
        organization_id=call.organization_id,
        email_domain_id=custom.id,
        direction="inbound",
        from_address=user.email,
        from_verified=True,
        to_addresses=["noreply@team.example.test"],
        subject="Custom inbox",
        body_text="My order details",
        status="received",
    )
    async_session.add(message)
    await async_session.commit()
    result = await post(client, "/internal/agent/inbox", {"call_id": str(call.id)})
    assert result.json()["quoted_untrusted_messages"][0]["id"] == str(message.id)


@pytest.mark.asyncio
async def test_verified_outbound_phone_run_can_return_its_original_file(
    client, async_session, monkeypatch, email_mock
):
    call, domain = await seed(async_session)
    source, att = await mail(async_session, call, domain)
    number = (await async_session.execute(select(PhoneNumber))).scalar_one()
    user = (await async_session.execute(select(User))).scalar_one()
    user.phone_number = "+12125550123"
    call.transport = "phone"
    call.direction = "outbound"
    call.from_number_id = number.id
    call.from_e164 = number.e164
    call.to_e164 = user.phone_number
    async_session.add(
        AccountCredit(
            organization_id=call.organization_id,
            kind="credit",
            channel="credit",
            amount_cents=1000,
            source="test",
        )
    )
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = b"pngdata"
    monkeypatch.setattr(emails, "get_s3_mail", lambda: s3)
    result = await post(
        client,
        "/internal/agent/send-email",
        {
            "call_id": str(call.id),
            "tool_invocation_id": str(uuid.uuid4()),
            "recipient_name": "you",
            "subject": "Original",
            "body_text": "Your file.",
            "reply_to_email_id": str(source.id),
            "attachment_ids": [str(att.id)],
        },
    )
    assert result.json()["ok"] is True, result.text
    assert (
        email_mock.send_email.call_args.kwargs["attachments"][0].payload == b"pngdata"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["browser", "phone"])
async def test_forged_sender_cannot_plant_history_or_files(
    client, async_session, monkeypatch, transport
):
    call, domain = await seed(async_session)
    source, att = await mail(async_session, call, domain)
    path = "/internal/browser-sessions/inbox"
    if transport == "phone":
        number = (await async_session.execute(select(PhoneNumber))).scalar_one()
        user = (await async_session.execute(select(User))).scalar_one()
        user.phone_number = "+12125550123"
        call.transport = "phone"
        call.direction = "outbound"
        call.from_number_id = number.id
        call.from_e164 = number.e164
        call.to_e164 = user.phone_number
        path = "/internal/agent/inbox"
    source.from_verified = False
    await async_session.commit()
    s3 = AsyncMock()
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    response = await post(client, path, {"call_id": str(call.id)})
    assert response.json()["quoted_untrusted_messages"] == []
    response = await post(
        client,
        path,
        {
            "call_id": str(call.id),
            "attachment_id": str(att.id),
            "attachment_format": "inspection_v1",
        },
    )
    assert "attachment_data" not in response.json()
    s3.fetch_raw.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_history_applies_common_trust_and_keeps_its_own_sends(
    client, async_session
):
    call, domain = await seed(async_session)
    trusted, _ = await mail(async_session, call, domain)
    refused, _ = await mail(
        async_session, call, domain, message_id="<auto@example.test>"
    )
    refused.metadata_ = {"agent_skip_reason": "auto_generated"}
    sent = Email(
        organization_id=call.organization_id,
        email_domain_id=domain.id,
        direction="outbound",
        from_address=domain.domain,
        to_addresses=[trusted.from_address],
        subject="Your receipt",
        body_text="This call sent your receipt.",
        status="sent",
        metadata_={"call_id": str(call.id), "tool_invocation_id": str(uuid.uuid4())},
    )
    forged_reply = Email(
        organization_id=call.organization_id,
        email_domain_id=domain.id,
        direction="outbound",
        from_address=domain.domain,
        to_addresses=[trusted.from_address],
        subject="Untrusted reply",
        body_text="This reply quotes refused mail.",
        status="sent",
        metadata_={"reply_to_email_id": str(refused.id)},
    )
    async_session.add_all([sent, forged_reply])
    await async_session.commit()
    response = await post(
        client, "/internal/browser-sessions/inbox", {"call_id": str(call.id)}
    )
    ids = {item["id"] for item in response.json()["quoted_untrusted_messages"]}
    assert ids == {str(trusted.id), str(sent.id)}


@pytest.mark.asyncio
async def test_text_attachment_rejects_a_cursor_inside_a_utf8_character(
    client, async_session, monkeypatch
):
    call, domain = await seed(async_session)
    _, att = await mail(async_session, call, domain)
    att.content_type = "text/plain"
    raw = "éfinal".encode()
    att.size_bytes = len(raw)
    await async_session.commit()
    s3 = AsyncMock()
    s3.fetch_raw.return_value = raw
    monkeypatch.setattr(deps, "get_s3_mail_optional", lambda: s3)
    result = await post(
        client,
        "/internal/browser-sessions/inbox",
        {"call_id": str(call.id), "attachment_id": str(att.id), "text_offset": 1},
    )
    assert result.status_code == 200, result.text
    assert "text position" in result.json()["spoken"]
    assert "continuation" not in result.json()
