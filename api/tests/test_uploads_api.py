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
from hailhq.core.file_uploads import (
    LINK_SWEEP_GRACE,
    MAX_PENDING_LINKS,
    VERIFICATION_MAX_FILE_BYTES,
)
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
    s3.put_attachment.assert_awaited_once()
    key, body, stored_type = s3.put_attachment.await_args.args
    # Each PUT writes its own key under the link's.
    assert key.startswith(f"verification-uploads/{org_id}/{link['id']}.")
    assert body == _JPEG
    # The declared type is stored, not the request header.
    assert stored_type == "image/jpeg"
    row = await async_session.get(FileUpload, UUID(link["id"]))
    await async_session.refresh(row)
    assert row.s3_key == key
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
    key, body, stored_type = s3.put_attachment.await_args.args
    assert key.startswith(f"outbound-attachments/{org_id}/{link['id']}.")
    assert (body, stored_type) == (b"a,b\n1,2\n", "text/csv")


async def test_two_concurrent_puts_only_one_wins(
    client, org_and_key, s3, session_factory, async_session
) -> None:
    """Both write their own key; the first update wins, the other PUT gets
    404 and deletes its object, so the row points at the winner's bytes."""
    from hailhq.core.db import get_session

    _, _, plain = org_and_key
    link = await _link(client, plain)
    entered, release = asyncio.Event(), asyncio.Event()
    keys: list[str] = []

    async def put(key, *_args):
        keys.append(key)
        if len(keys) == 1:
            entered.set()
            await release.wait()

    s3.put_attachment.side_effect = put

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
        first_resp = await first
    finally:
        release.set()
        app.dependency_overrides[get_session] = saved
    assert second.status_code == 200
    assert first_resp.status_code == 404
    assert len(set(keys)) == 2
    s3.delete.assert_awaited_once_with(keys[0])
    row = await async_session.get(FileUpload, UUID(link["id"]))
    await async_session.refresh(row)
    assert row.s3_key == keys[1]


async def test_a_busy_server_is_503_and_keeps_the_link(
    client, org_and_key, s3, monkeypatch
) -> None:
    """Too many body reads at once: 503, and the link still works."""
    from hailhq.api.routes import uploads

    _, _, plain = org_and_key
    link = await _link(client, plain)
    monkeypatch.setattr(uploads, "MAX_CONCURRENT_BODY_READS", 0)
    busy = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert busy.status_code == 503
    assert busy.headers["Retry-After"] == "5"
    monkeypatch.setattr(uploads, "MAX_CONCURRENT_BODY_READS", 1)
    ok = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert ok.status_code == 200


async def test_a_failed_storage_write_leaves_the_link_usable(
    client, org_and_key, s3
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    s3.put_attachment.side_effect = RuntimeError("s3 down")
    failed = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert failed.status_code == 503
    s3.put_attachment.side_effect = None
    ok = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert ok.status_code == 200


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


def test_put_is_rate_limited_by_client_address() -> None:
    """No bearer: the general limiter keys the PUT on the caller's address."""
    from hailhq.api.ratelimit import _is_exempt, _rate_limit_key
    from starlette.requests import Request

    assert not _is_exempt("/v1/uploads/abc", "PUT")
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": "/v1/uploads/abc",
            "headers": [],
            "client": ("203.0.113.9", 5000),
        }
    )
    assert _rate_limit_key(request) == "203.0.113.9"


async def test_create_refuses_a_filename_with_control_characters(
    client, org_and_key, s3
) -> None:
    _, _, plain = org_and_key
    resp = await client.post(
        "/v1/uploads",
        json={
            "purpose": "email_attachment",
            "filename": "a.pdf\r\nBcc: x@example.com",
            "content_type": "application/pdf",
        },
        headers=_auth(plain),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "filename"]


async def test_making_a_link_does_not_wait_for_the_org_lock(
    client, org_and_key, s3, session_factory
) -> None:
    """The org lock is held through SMS carrier calls; links use their own."""
    from hailhq.core.db import get_session, org_lock

    org_id, _, plain = org_and_key

    async def per_request_session():
        async with session_factory() as session:
            yield session

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    try:
        async with session_factory() as holder:
            await org_lock(holder, org_id)
            resp = await asyncio.wait_for(
                client.post(
                    "/v1/uploads",
                    json={
                        "purpose": "email_attachment",
                        "filename": "a.csv",
                        "content_type": "text/csv",
                    },
                    headers=_auth(plain),
                ),
                5,
            )
            await holder.rollback()
    finally:
        app.dependency_overrides[get_session] = saved
    assert resp.status_code == 201, resp.text


async def test_two_links_at_once_never_pass_the_cap(
    client, org_and_key, s3, async_session, session_factory
) -> None:
    """The cap is still exact: two links made together share one lock."""
    from hailhq.core.db import get_session

    org_id, _, plain = org_and_key
    now = datetime.now(timezone.utc)
    for _ in range(MAX_PENDING_LINKS - 1):
        async_session.add(
            FileUpload(
                organization_id=org_id,
                filename="a",
                content_type="text/plain",
                s3_key="k",
                token_hash="h",
                link_expires_at=now + timedelta(minutes=10),
            )
        )
    await async_session.commit()

    async def per_request_session():
        async with session_factory() as session:
            yield session

    body = {"purpose": "email_attachment", "filename": "a", "content_type": "a/b"}
    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    try:
        results = await asyncio.gather(
            *(
                client.post("/v1/uploads", json=body, headers=_auth(plain))
                for _ in range(2)
            )
        )
    finally:
        app.dependency_overrides[get_session] = saved
    assert sorted(r.status_code for r in results) == [201, 429]


async def test_a_link_removed_during_its_put_is_410_and_leaves_no_object(
    client, org_and_key, s3, async_session
) -> None:
    """The row went away while the body was stored: the object is removed,
    and the caller is told to make a new link."""
    from sqlalchemy import delete

    org_id, _, plain = org_and_key
    link = await _link(client, plain)
    upload_id = UUID(link["id"])

    async def swept(*_args):
        await async_session.execute(
            delete(FileUpload).where(FileUpload.id == upload_id)
        )
        await async_session.commit()

    s3.put_attachment.side_effect = swept
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 410, resp.text
    key = s3.put_attachment.await_args.args[0]
    assert key.startswith(f"verification-uploads/{org_id}/{upload_id}.")
    s3.delete.assert_awaited_once_with(key)


async def test_a_wrong_token_and_a_used_link_share_one_404(
    client, org_and_key, s3
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    wrong = await client.put(f"/v1/uploads/{link['id']}?token=nope", content=_JPEG)
    assert (await client.put(_path(link["upload_url"]), content=_JPEG)).is_success
    used = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert wrong.json() == used.json()
    assert wrong.json()["detail"] == "upload link not found, or already used"


async def test_a_body_read_past_its_deadline_is_408_and_keeps_the_link(
    client, org_and_key, s3, monkeypatch
) -> None:
    from hailhq.api.routes import uploads as uploads_routes

    _, _, plain = org_and_key
    link = await _link(client, plain)
    monkeypatch.setattr(uploads_routes, "MAX_BODY_READ_SECONDS", 0.2)

    async def slow():
        yield _JPEG[:4]
        await asyncio.sleep(2)
        yield _JPEG[4:]

    resp = await client.put(_path(link["upload_url"]), content=slow())
    assert resp.status_code == 408, resp.text
    s3.put_attachment.assert_not_awaited()
    again = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert again.status_code == 200, again.text


def test_the_body_read_deadline_never_passes_the_link_expiry() -> None:
    from hailhq.api.routes.uploads import MAX_BODY_READ_SECONDS, _read_deadline

    now = datetime.now(timezone.utc)
    assert _read_deadline(now + timedelta(minutes=14), now) == MAX_BODY_READ_SECONDS
    assert _read_deadline(now + timedelta(seconds=30), now) == pytest.approx(30)


async def test_one_org_runs_at_most_two_body_reads(
    client, org_and_key, s3, async_session, session_factory
) -> None:
    """A third PUT from one org waits its turn; another org is not blocked."""
    from hailhq.core.db import get_session

    _, _, plain = org_and_key
    _, _, other = await insert_org_and_key(async_session, org_slug="other")
    links = [await _link(client, plain) for _ in range(3)]
    other_link = await _link(client, other)
    release = asyncio.Event()
    reading = asyncio.Semaphore(0)

    async def held():
        reading.release()
        await release.wait()
        yield _JPEG

    async def per_request_session():
        async with session_factory() as session:
            yield session

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    try:
        first = [
            asyncio.create_task(client.put(_path(link["upload_url"]), content=held()))
            for link in links[:2]
        ]
        for _ in first:
            await asyncio.wait_for(reading.acquire(), 5)
        third = await asyncio.wait_for(
            client.put(_path(links[2]["upload_url"]), content=_JPEG), 5
        )
        elsewhere = await asyncio.wait_for(
            client.put(_path(other_link["upload_url"]), content=_JPEG), 5
        )
        release.set()
        done = await asyncio.gather(*first)
    finally:
        release.set()
        app.dependency_overrides[get_session] = saved
    assert third.status_code == 503
    assert third.headers["Retry-After"]
    assert elsewhere.status_code == 200, elsewhere.text
    assert [r.status_code for r in done] == [200, 200]
    # The busy org's third link was not used up.
    retry = await client.put(_path(links[2]["upload_url"]), content=_JPEG)
    assert retry.status_code == 200, retry.text


async def test_puts_over_the_global_cap_get_503_at_once(
    client, org_and_key, s3, async_session, session_factory, monkeypatch
) -> None:
    """No queueing: past the process cap a PUT answers 503 without waiting,
    and its link is not claimed."""
    from hailhq.api.routes import uploads as uploads_routes
    from hailhq.core.db import get_session

    monkeypatch.setattr(uploads_routes, "MAX_CONCURRENT_BODY_READS", 2)
    keys = [org_and_key[2]]
    for i in range(3):
        keys.append((await insert_org_and_key(async_session, org_slug=f"o{i}"))[2])
    links = [await _link(client, k) for k in keys]
    release = asyncio.Event()
    reading = asyncio.Semaphore(0)

    async def held():
        reading.release()
        await release.wait()
        yield _JPEG

    async def per_request_session():
        async with session_factory() as session:
            yield session

    saved = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = per_request_session
    try:
        first = [
            asyncio.create_task(client.put(_path(link["upload_url"]), content=held()))
            for link in links[:2]
        ]
        for _ in first:
            await asyncio.wait_for(reading.acquire(), 5)
        over = await asyncio.wait_for(
            asyncio.gather(
                *(
                    client.put(_path(link["upload_url"]), content=_JPEG)
                    for link in links[2:]
                )
            ),
            1,
        )
        release.set()
        done = await asyncio.gather(*first)
    finally:
        release.set()
        app.dependency_overrides[get_session] = saved
    assert [r.status_code for r in over] == [503, 503]
    assert all(r.headers["Retry-After"] for r in over)
    assert [r.status_code for r in done] == [200, 200]
    assert uploads_routes._reads_total == 0
    assert uploads_routes._org_reads == {}
    # Not claimed: the refused links still work.
    for link in links[2:]:
        again = await client.put(_path(link["upload_url"]), content=_JPEG)
        assert again.status_code == 200, again.text


async def test_a_failed_final_commit_leaves_the_link_usable(
    client, org_and_key, s3, async_session, monkeypatch
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    stored = asyncio.Event()

    async def put(*_args):
        stored.set()

    s3.put_attachment.side_effect = put
    real_commit = async_session.commit
    failed = []

    async def commit():
        if stored.is_set() and not failed:
            failed.append(True)
            raise RuntimeError("database went away")
        await real_commit()

    monkeypatch.setattr(async_session, "commit", commit)
    with pytest.raises(RuntimeError):
        await client.put(_path(link["upload_url"]), content=_JPEG)
    assert failed
    # A request's session closes at its end, which rolls back; the tests
    # share one session, so do that here.
    await async_session.rollback()
    s3.put_attachment.side_effect = None
    again = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert again.status_code == 200, again.text


def _clock_for_final_update(monkeypatch, at: datetime) -> None:
    """``_now`` is real for the expiry check and the read deadline, then
    ``at`` for the final update and anything after it."""
    from hailhq.api.routes import uploads as uploads_routes

    real_now = uploads_routes._now
    calls = []

    def clock():
        calls.append(1)
        return real_now() if len(calls) <= 2 else at

    monkeypatch.setattr(uploads_routes, "_now", clock)


def _expires(link: dict) -> datetime:
    return datetime.fromisoformat(link["expires_at"].replace("Z", "+00:00"))


async def test_a_put_that_starts_in_time_is_stored_after_the_expiry(
    client, org_and_key, s3, monkeypatch
) -> None:
    """The body arrived before the expiry, the update runs after it but
    inside the store window: the file is stored."""
    _, _, plain = org_and_key
    link = await _link(client, plain)
    _clock_for_final_update(
        monkeypatch, _expires(link) + LINK_SWEEP_GRACE - timedelta(minutes=5)
    )
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 200, resp.text
    s3.delete.assert_not_awaited()


async def test_the_store_window_includes_its_last_instant(
    client, org_and_key, s3, monkeypatch
) -> None:
    """Exactly ``LINK_SWEEP_GRACE`` after the expiry the PUT may still store;
    the sweep takes a link only strictly after that."""
    _, _, plain = org_and_key
    link = await _link(client, plain)
    _clock_for_final_update(monkeypatch, _expires(link) + LINK_SWEEP_GRACE)
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 200, resp.text


async def test_storing_past_the_window_is_410_and_leaves_no_object(
    client, org_and_key, s3, monkeypatch
) -> None:
    """One microsecond past the window the sweep may own the row, so the PUT
    does not mark it used."""
    _, _, plain = org_and_key
    link = await _link(client, plain)
    _clock_for_final_update(
        monkeypatch,
        _expires(link) + LINK_SWEEP_GRACE + timedelta(microseconds=1),
    )
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 410, resp.text
    s3.delete.assert_awaited_once_with(s3.put_attachment.await_args.args[0])


async def test_a_put_that_lost_to_another_is_404_even_past_the_window(
    client, org_and_key, s3, monkeypatch, async_session
) -> None:
    """The link was already used: 404 (not 410, which would ask for a new
    link and a second upload of a file already stored)."""
    _, _, plain = org_and_key
    link = await _link(client, plain)
    assert (await client.put(_path(link["upload_url"]), content=_JPEG)).is_success
    row = await async_session.get(FileUpload, UUID(link["id"]))
    await async_session.refresh(row)
    # Give the used link its token back, as if a second PUT read it in time.
    from urllib.parse import parse_qs, urlparse

    from hailhq.core.file_uploads import hash_token

    token = parse_qs(urlparse(link["upload_url"]).query)["token"][0]
    row.token_hash = hash_token(token)
    await async_session.commit()
    _clock_for_final_update(monkeypatch, _expires(link) + timedelta(hours=1))
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 404, resp.text


def test_the_documented_upload_times() -> None:
    """The PUT's description says 10 and 15 minutes in plain words."""
    from hailhq.api.routes.uploads import MAX_BODY_READ_SECONDS

    assert MAX_BODY_READ_SECONDS == 600
    assert LINK_SWEEP_GRACE == timedelta(minutes=15)


async def test_the_winner_deletes_earlier_attempts(client, org_and_key, s3) -> None:
    """An earlier PUT cut off after its S3 write left an object; the PUT that
    marks the link used deletes every other object under the link's key."""
    org_id, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 200, resp.text
    key = s3.put_attachment.await_args.args[0]
    s3.delete_prefix.assert_awaited_once_with(
        f"verification-uploads/{org_id}/{link['id']}", keep=key
    )
