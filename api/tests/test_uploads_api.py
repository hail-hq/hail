"""POST /uploads and PUT /uploads/{id}: one-time upload links."""

from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

import httpx
import pytest
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.api.routes import emails as emails_routes
from hailhq.core.config import settings
from hailhq.core.file_uploads import MAX_PENDING_LINKS, VERIFICATION_MAX_FILE_BYTES
from hailhq.core.models import FileUpload
from sqlalchemy import select, update

from .conftest import insert_org_and_key

_JPEG = b"\xff\xd8\xff\xe0" + b"j" * 100
_PDF = b"%PDF-1.4 a pdf"


@pytest.fixture()
def s3(monkeypatch: pytest.MonkeyPatch):
    store = AsyncMock()
    store.fetch_raw.return_value = _PDF
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    monkeypatch.setattr(emails_routes, "get_s3_mail", lambda: store)
    monkeypatch.setattr(settings, "hail_api_url", "https://api.example.test/")
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)


def _auth(plain: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plain}"}


async def _link(
    client: httpx.AsyncClient,
    plain: str,
    purpose: str = "verification_document",
    content_type: str = "image/jpeg",
    filename: str = "passport.jpg",
) -> dict:
    resp = await client.post(
        "/v1/uploads",
        json={"purpose": purpose, "filename": filename, "content_type": content_type},
        headers=_auth(plain),
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _path(upload_url: str) -> str:
    parts = urlsplit(upload_url)
    return f"{parts.path}?{parts.query}"


async def test_create_returns_a_one_time_link(client, org_and_key, s3) -> None:
    _, _, plain = org_and_key
    body = await _link(client, plain)
    assert body["purpose"] == "verification_document"
    assert body["method"] == "PUT"
    assert body["content_type"] == "image/jpeg"
    assert body["max_bytes"] == VERIFICATION_MAX_FILE_BYTES
    parts = urlsplit(body["upload_url"])
    assert f"{parts.scheme}://{parts.netloc}" == "https://api.example.test"
    assert parts.path == f"/v1/uploads/{body['id']}"
    token = parse_qs(parts.query)["token"][0]
    assert len(token) >= 43
    expires = datetime.fromisoformat(body["expires_at"])
    left = expires - datetime.now(timezone.utc)
    assert timedelta(minutes=14) < left <= timedelta(minutes=15)


async def test_create_stores_only_the_token_hash(
    client, org_and_key, s3, async_session
) -> None:
    _, _, plain = org_and_key
    body = await _link(client, plain)
    token = parse_qs(urlsplit(body["upload_url"]).query)["token"][0]
    row = await async_session.get(FileUpload, UUID(body["id"]))
    assert row.token_hash == hashlib.sha256(token.encode()).hexdigest()
    assert row.uploaded_at is None and row.size_bytes is None
    assert row.s3_key == f"verification-uploads/{row.organization_id}/{row.id}"


async def test_create_rejects_other_types_for_verification(
    client, org_and_key, s3
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/v1/uploads",
        json={
            "purpose": "verification_document",
            "filename": "a.gif",
            "content_type": "image/gif",
        },
        headers=_auth(plain),
    )
    assert resp.status_code == 422


@pytest.mark.parametrize(
    "content_type", ["text/plain; charset=utf-8", "x'; rm -rf ~", "pdf"]
)
async def test_create_rejects_a_content_type_that_is_not_bare(
    client, org_and_key, s3, content_type
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/v1/uploads",
        json={
            "purpose": "email_attachment",
            "filename": "a",
            "content_type": content_type,
        },
        headers=_auth(plain),
    )
    assert resp.status_code == 422


async def test_create_needs_auth(client, s3) -> None:
    resp = await client.post(
        "/v1/uploads",
        json={
            "purpose": "email_attachment",
            "filename": "a",
            "content_type": "text/plain",
        },
    )
    assert resp.status_code == 401


async def test_create_is_503_without_a_bucket(client, org_and_key, monkeypatch) -> None:
    _, _, plain = org_and_key
    monkeypatch.setattr(settings, "hail_mail_name_prefix", "")
    resp = await client.post(
        "/v1/uploads",
        json={
            "purpose": "email_attachment",
            "filename": "a",
            "content_type": "text/plain",
        },
        headers=_auth(plain),
    )
    assert resp.status_code == 503


async def test_the_51st_pending_link_is_429(
    client, org_and_key, s3, async_session
) -> None:
    org_id, _, plain = org_and_key
    now = datetime.now(timezone.utc)
    for _ in range(MAX_PENDING_LINKS):
        async_session.add(
            FileUpload(
                organization_id=org_id,
                purpose="email_attachment",
                filename="a",
                content_type="text/plain",
                s3_key=f"outbound-attachments/{org_id}/{uuid4()}",
                token_hash="h",
                link_expires_at=now + timedelta(minutes=10),
            )
        )
    # Expired and used links do not count.
    async_session.add(
        FileUpload(
            organization_id=org_id,
            filename="a",
            content_type="text/plain",
            s3_key="k",
            token_hash="h",
            link_expires_at=now - timedelta(minutes=1),
        )
    )
    await async_session.commit()
    resp = await client.post(
        "/v1/uploads",
        json={
            "purpose": "email_attachment",
            "filename": "a",
            "content_type": "text/plain",
        },
        headers=_auth(plain),
    )
    assert resp.status_code == 429
    # Another organization is not affected.
    _, _, other = await insert_org_and_key(async_session, org_slug="other")
    await _link(client, other)


async def test_put_stores_the_bytes_and_reports_size_and_sha256(
    client, org_and_key, s3, async_session
) -> None:
    org_id, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(
        _path(link["upload_url"]),
        content=_JPEG,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "id": link["id"],
        "purpose": "verification_document",
        "filename": "passport.jpg",
        "content_type": "image/jpeg",
        "size_bytes": len(_JPEG),
        "sha256": hashlib.sha256(_JPEG).hexdigest(),
    }
    key = f"verification-uploads/{org_id}/{link['id']}"
    # The declared type is stored, not the request header.
    s3.put_attachment.assert_awaited_once_with(key, _JPEG, "image/jpeg")
    row = await async_session.get(FileUpload, UUID(link["id"]))
    await async_session.refresh(row)
    assert row.token_hash is None
    assert row.uploaded_at is not None
    assert row.sha256 == hashlib.sha256(_JPEG).hexdigest()


async def test_put_works_only_once(client, org_and_key, s3) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    first = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert first.status_code == 200
    second = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert second.status_code == 404
    assert s3.put_attachment.await_count == 1


async def test_put_with_a_wrong_token_is_404(client, org_and_key, s3) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(f"/v1/uploads/{link['id']}?token=nope", content=_JPEG)
    assert resp.status_code == 404
    missing = await client.put(f"/v1/uploads/{uuid4()}?token=nope", content=_JPEG)
    assert missing.status_code == 404
    # One message for every case: the response does not say which.
    assert resp.json() == missing.json()
    s3.put_attachment.assert_not_awaited()


async def test_put_after_expiry_is_410(client, org_and_key, s3, async_session) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    await async_session.execute(
        update(FileUpload)
        .where(FileUpload.id == UUID(link["id"]))
        .values(link_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await async_session.commit()
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 410
    # A wrong token on an expired link is still 404.
    wrong = await client.put(f"/v1/uploads/{link['id']}?token=nope", content=_JPEG)
    assert wrong.status_code == 404
    s3.put_attachment.assert_not_awaited()


async def test_put_too_large_is_413(client, org_and_key, s3) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    big = _JPEG + b"x" * VERIFICATION_MAX_FILE_BYTES
    resp = await client.put(_path(link["upload_url"]), content=big)
    assert resp.status_code == 413
    s3.put_attachment.assert_not_awaited()
    # The link still works for a file within the limit.
    ok = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert ok.status_code == 200


async def test_put_too_large_without_content_length_is_413(
    client, org_and_key, s3
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)

    async def chunks():
        yield _JPEG
        for _ in range(11):
            yield b"x" * (1024 * 1024)

    resp = await client.put(_path(link["upload_url"]), content=chunks())
    assert resp.status_code == 413
    s3.put_attachment.assert_not_awaited()


async def test_put_empty_is_422(client, org_and_key, s3) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(_path(link["upload_url"]), content=b"")
    assert resp.status_code == 422


async def test_put_rejects_bytes_that_do_not_match_the_declared_type(
    client, org_and_key, s3
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(_path(link["upload_url"]), content=_PDF)
    assert resp.status_code == 422
    assert "image/jpeg" in resp.text
    s3.put_attachment.assert_not_awaited()


async def test_put_email_attachment_takes_any_bytes(client, org_and_key, s3) -> None:
    org_id, _, plain = org_and_key
    link = await _link(
        client, plain, purpose="email_attachment", content_type="text/csv"
    )
    resp = await client.put(_path(link["upload_url"]), content=b"a,b\n1,2\n")
    assert resp.status_code == 200, resp.text
    s3.put_attachment.assert_awaited_once_with(
        f"outbound-attachments/{org_id}/{link['id']}", b"a,b\n1,2\n", "text/csv"
    )


async def test_two_concurrent_puts_only_one_wins(
    client, org_and_key, s3, session_factory
) -> None:
    """The second PUT finds the link claimed while the first writes S3."""
    from hailhq.core.db import get_session

    _, _, plain = org_and_key
    link = await _link(client, plain)
    entered, release = asyncio.Event(), asyncio.Event()

    async def slow_put(*_args):
        entered.set()
        await release.wait()

    s3.put_attachment.side_effect = slow_put

    async def per_request_session():
        async with session_factory() as session:
            yield session

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    try:
        first = asyncio.create_task(
            client.put(_path(link["upload_url"]), content=_JPEG)
        )
        await asyncio.wait_for(entered.wait(), 5)
        second = await asyncio.wait_for(
            client.put(_path(link["upload_url"]), content=_JPEG), 5
        )
        release.set()
        assert (await first).status_code == 200
    finally:
        release.set()
        app.dependency_overrides[get_session] = saved
    assert second.status_code == 404
    assert s3.put_attachment.await_count == 1


async def test_put_needs_no_authorization_header(client, org_and_key, s3) -> None:
    """The token is the credential; a bearer is neither needed nor used."""
    _, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(
        _path(link["upload_url"]),
        content=_JPEG,
        headers={"Authorization": "Bearer not-a-key"},
    )
    assert resp.status_code == 200


async def _sender(client, plain) -> None:
    created = await client.post(
        "/email-domains",
        json={"kind": "custom", "domain": "acme.com"},
        headers=_auth(plain),
    )
    assert created.status_code == 201
    await client.post(
        f"/email-domains/{created.json()['id']}/verify", headers=_auth(plain)
    )


async def _send(client, plain, attachment_id) -> httpx.Response:
    return await client.post(
        "/v1/emails",
        json={
            "to": ["alice@example.com"],
            "subject": "hi",
            "body_text": "body",
            "recipient_consent": True,
            "attachment_ids": [attachment_id],
        },
        headers=_auth(plain),
    )


async def test_an_email_upload_works_in_attachment_ids(
    client, org_and_key, s3, email_mock, async_session
) -> None:
    _, _, plain = org_and_key
    await _sender(client, plain)
    link = await _link(
        client,
        plain,
        purpose="email_attachment",
        content_type="application/pdf",
        filename="invoice.pdf",
    )
    # Not uploaded yet: not found.
    assert (await _send(client, plain, link["id"])).status_code == 404
    put = await client.put(_path(link["upload_url"]), content=_PDF)
    assert put.status_code == 200
    resp = await _send(client, plain, link["id"])
    assert resp.status_code == 201, resp.text
    attachment = email_mock.send_email.call_args.kwargs["attachments"][0]
    assert attachment.filename == "invoice.pdf"
    assert attachment.content_type == "application/pdf"
    row = (
        await async_session.execute(
            select(FileUpload).where(FileUpload.id == UUID(link["id"]))
        )
    ).scalar_one()
    await async_session.refresh(row)
    assert row.first_used_at is not None


async def test_a_verification_document_is_not_an_attachment(
    client, org_and_key, s3, email_mock
) -> None:
    _, _, plain = org_and_key
    await _sender(client, plain)
    link = await _link(client, plain)
    assert (
        await client.put(_path(link["upload_url"]), content=_JPEG)
    ).status_code == 200
    assert (await _send(client, plain, link["id"])).status_code == 404
    email_mock.send_email.assert_not_awaited()


def test_only_the_upload_put_skips_the_general_limit() -> None:
    """The PUT carries its own token; every no-bearer caller shares the proxy IP."""
    from hailhq.api.ratelimit import _is_exempt

    assert _is_exempt("/v1/uploads/abc", "PUT")
    assert _is_exempt("/uploads/abc", "PUT")
    assert not _is_exempt("/v1/uploads", "POST")
    assert not _is_exempt("/v1/uploads/abc", "GET")
    assert not _is_exempt("/v1/uploads/abc", "DELETE")


def test_evidence_links_still_exempt_reads_only() -> None:
    from hailhq.api.ratelimit import _is_exempt

    assert _is_exempt("/v1/verifications/sms/evidence/abc", "GET")
    assert not _is_exempt("/v1/verifications/sms/evidence/abc", "PUT")
