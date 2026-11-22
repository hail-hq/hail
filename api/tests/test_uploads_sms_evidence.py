"""Upload links with purpose ``sms_evidence``: the PUT returns the same
public evidence link as ``POST /verifications/sms/evidence``."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from uuid import UUID

import httpx
import pytest
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.api.routes.sms_evidence import get_evidence_store
from hailhq.core.config import settings
from hailhq.core.email_attachment_gc import EmailAttachmentGcWorker
from hailhq.core.file_uploads import LINK_SWEEP_GRACE
from hailhq.core.models import FileUpload
from hailhq.core.sms_evidence import MAX_EVIDENCE_BYTES, MAX_EVIDENCE_FILES
from sqlalchemy import select, update

_PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 64
_JPEG = b"\xff\xd8\xff\xe0" + b"j" * 64


class FakeStore:
    """The mail bucket in memory, so keys and links are checked for real."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.extra = 0  # files another request stores meanwhile

    async def put_attachment(self, key, data, content_type, encrypt=False) -> None:
        self.objects[key] = (data, content_type)

    async def count_prefix(self, prefix: str, up_to: int) -> int:
        found = sum(1 for key in self.objects if key.startswith(prefix))
        return min(found + self.extra, up_to)

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str, keep: str | None = None) -> int:
        gone = [k for k in self.objects if k.startswith(prefix) and k != keep]
        for key in gone:
            del self.objects[key]
        return len(gone)

    async def presign_get(self, key: str, ttl_seconds: int) -> str:
        return f"https://storage.example.test/{key}"


@pytest.fixture()
def store(monkeypatch: pytest.MonkeyPatch):
    fake = FakeStore()
    app.dependency_overrides[get_s3_mail_optional] = lambda: fake
    app.dependency_overrides[get_evidence_store] = lambda: fake
    monkeypatch.setattr(settings, "hail_api_url", "https://api.example.com/")
    try:
        yield fake
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)
        app.dependency_overrides.pop(get_evidence_store, None)


def _auth(plain: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plain}"}


def _path(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.path}?{parts.query}" if parts.query else parts.path


async def _make_link(
    client: httpx.AsyncClient, plain: str, content_type: str = "image/png"
) -> httpx.Response:
    return await client.post(
        "/v1/uploads",
        json={
            "purpose": "sms_evidence",
            "filename": "signup-form.png",
            "content_type": content_type,
        },
        headers=_auth(plain),
    )


async def _link(client, plain, content_type: str = "image/png") -> dict:
    resp = await _make_link(client, plain, content_type)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_the_put_returns_a_public_link_that_opens_and_deletes(
    client, org_and_key, store, async_session
) -> None:
    org_id, _, plain = org_and_key
    link = await _link(client, plain)
    assert link["purpose"] == "sms_evidence"
    assert link["max_bytes"] == MAX_EVIDENCE_BYTES

    resp = await client.put(_path(link["upload_url"]), content=_PNG)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["purpose"] == "sms_evidence"
    assert body["content_type"] == "image/png"
    assert body["size_bytes"] == len(_PNG)
    assert body["sha256"] == hashlib.sha256(_PNG).hexdigest()
    prefix = f"https://api.example.com/v1/verifications/sms/evidence/{org_id}/"
    assert body["url"].startswith(prefix)
    token = body["url"].removeprefix(prefix)
    assert len(token) == 32
    # Stored where the evidence route keeps its files.
    assert store.objects == {
        f"sms-evidence/{org_id}/{token}": (_PNG, "image/png"),
    }

    # The link works through the real public route, with no sign-in.
    opened = await client.get(_path(body["url"]))
    assert opened.status_code == 302
    assert opened.headers["location"].endswith(f"sms-evidence/{org_id}/{token}")

    # The link row is gone: no table keeps evidence files.
    rows = (
        await async_session.execute(
            select(FileUpload).where(FileUpload.id == UUID(link["id"]))
        )
    ).all()
    assert rows == []
    # Used once only.
    again = await client.put(_path(link["upload_url"]), content=_PNG)
    assert again.status_code == 404

    deleted = await client.delete(_path(body["url"]), headers=_auth(plain))
    assert deleted.status_code == 204
    assert store.objects == {}


async def test_sweeping_an_unused_link_keeps_the_evidence(
    client, org_and_key, store, session_factory, async_session
) -> None:
    _, _, plain = org_and_key
    used = await _link(client, plain)
    resp = await client.put(_path(used["upload_url"]), content=_PNG)
    assert resp.status_code == 200, resp.text
    unused = await _link(client, plain)
    await async_session.execute(
        update(FileUpload)
        .where(FileUpload.id == UUID(unused["id"]))
        .values(
            link_expires_at=datetime.now(timezone.utc)
            - LINK_SWEEP_GRACE
            - timedelta(minutes=1)
        )
    )
    await async_session.commit()
    worker = EmailAttachmentGcWorker(
        session_factory=session_factory, s3_factory=lambda: store
    )
    assert await worker.tick() == 1
    # The sweep deletes the unused link's prefix, which names no file.
    assert len(store.objects) == 1


async def test_a_jpeg_link_takes_a_jpeg(client, org_and_key, store) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain, "image/jpeg")
    resp = await client.put(_path(link["upload_url"]), content=_JPEG)
    assert resp.status_code == 200, resp.text
    assert resp.json()["content_type"] == "image/jpeg"


@pytest.mark.parametrize(
    "content",
    [
        b"<html><script>alert(1)</script></html>",
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b"GIF89a",
        # A PDF header after other bytes: verification documents allow it,
        # evidence does not.
        b"junk %PDF-1.4",
    ],
)
async def test_the_put_refuses_other_files(client, org_and_key, store, content) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain, "application/pdf")
    resp = await client.put(_path(link["upload_url"]), content=content)
    assert resp.status_code == 422
    assert store.objects == {}
    # The link still works for a real file.
    ok = await client.put(_path(link["upload_url"]), content=b"%PDF-1.4 a pdf")
    assert ok.status_code == 200, ok.text


async def test_the_put_refuses_a_file_of_another_type(
    client, org_and_key, store
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain, "image/jpeg")
    resp = await client.put(_path(link["upload_url"]), content=_PNG)
    assert resp.status_code == 422
    assert "image/jpeg" in resp.text
    assert store.objects == {}


async def test_the_link_refuses_other_declared_types(
    client, org_and_key, store
) -> None:
    _, _, plain = org_and_key
    resp = await _make_link(client, plain, "image/svg+xml")
    assert resp.status_code == 422
    assert "sms_evidence" in resp.text


async def test_the_put_refuses_a_file_over_10_mb(client, org_and_key, store) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    resp = await client.put(
        _path(link["upload_url"]), content=_PNG + b"x" * MAX_EVIDENCE_BYTES
    )
    assert resp.status_code == 413
    assert store.objects == {}


async def test_a_full_account_gets_no_link(client, org_and_key, store) -> None:
    _, _, plain = org_and_key
    store.extra = MAX_EVIDENCE_FILES
    resp = await _make_link(client, plain)
    assert resp.status_code == 409
    assert "Delete one" in resp.json()["detail"]


async def test_an_account_that_filled_up_since_the_link_is_409(
    client, org_and_key, store
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    store.extra = MAX_EVIDENCE_FILES
    resp = await client.put(_path(link["upload_url"]), content=_PNG)
    assert resp.status_code == 409
    assert store.objects == {}


async def test_parallel_uploads_never_pass_the_cap(client, org_and_key, store) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    real_put = store.put_attachment

    async def put_while_another_lands(key, data, content_type, encrypt=False):
        await real_put(key, data, content_type)
        store.extra = MAX_EVIDENCE_FILES  # others landed at the same time

    store.put_attachment = put_while_another_lands
    resp = await client.put(_path(link["upload_url"]), content=_PNG)
    assert resp.status_code == 409
    # This PUT's file is taken back.
    assert store.objects == {}


@pytest.mark.parametrize("base", ["http://localhost:8080", "https://hail.local"])
async def test_a_server_reviewers_cannot_reach_gives_no_link(
    client, org_and_key, store, monkeypatch, base
) -> None:
    _, _, plain = org_and_key
    monkeypatch.setattr(settings, "hail_api_url", base)
    resp = await _make_link(client, plain)
    assert resp.status_code == 503
    assert "HAIL_API_URL" in resp.json()["detail"]


async def test_a_storage_failure_keeps_the_link_and_leaves_no_file(
    client, org_and_key, store
) -> None:
    _, _, plain = org_and_key
    link = await _link(client, plain)
    real_count = store.count_prefix
    calls = 0

    async def fail_after_the_write(prefix, up_to):
        nonlocal calls
        calls += 1
        if calls == 2:  # the recount after the write
            raise RuntimeError("storage down")
        return await real_count(prefix, up_to)

    store.count_prefix = fail_after_the_write
    resp = await client.put(_path(link["upload_url"]), content=_PNG)
    assert resp.status_code == 503
    assert store.objects == {}
    store.count_prefix = real_count
    ok = await client.put(_path(link["upload_url"]), content=_PNG)
    assert ok.status_code == 200, ok.text


async def test_a_storage_failure_while_counting_is_503(
    client, org_and_key, store
) -> None:
    _, _, plain = org_and_key

    async def down(prefix, up_to):
        raise RuntimeError("storage down")

    store.count_prefix = down
    resp = await _make_link(client, plain)
    assert resp.status_code == 503
    assert "try again" in resp.json()["detail"]
