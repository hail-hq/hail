"""Integration tests for POST /email-attachments."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from io import BytesIO
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from fastapi import UploadFile
from hailhq.api.deps import Principal, get_s3_mail
from hailhq.api.main import app
from hailhq.api.routes.email_attachments import create_email_attachment
from hailhq.core.email_attachment_limits import MAX_EMAIL_ATTACHMENT_BYTES
from hailhq.core.models import EmailAttachmentUpload, OrgClosure
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from .conftest import insert_org_and_key  # noqa: F401


@pytest.fixture()
def s3_mail_mock():
    s3 = AsyncMock()
    app.dependency_overrides[get_s3_mail] = lambda: s3
    try:
        yield s3
    finally:
        app.dependency_overrides.pop(get_s3_mail, None)


async def test_upload_returns_id_and_metadata(
    client: httpx.AsyncClient, org_and_key: tuple, s3_mail_mock: AsyncMock
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}

    resp = await client.post(
        "/email-attachments",
        files={"file": ("invoice.pdf", b"%PDF-1.4 fake pdf bytes", "application/pdf")},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["filename"] == "invoice.pdf"
    assert body["content_type"] == "application/pdf"
    assert body["size_bytes"] == len(b"%PDF-1.4 fake pdf bytes")
    assert body["id"]
    s3_mail_mock.put_attachment.assert_awaited_once()


async def test_upload_rejects_oversize_file(
    client: httpx.AsyncClient, org_and_key: tuple, s3_mail_mock: AsyncMock
) -> None:
    _, _, plain = org_and_key
    headers = {"Authorization": f"Bearer {plain}"}
    oversize = b"x" * (MAX_EMAIL_ATTACHMENT_BYTES + 1)

    resp = await client.post(
        "/email-attachments",
        files={"file": ("big.bin", oversize, "application/octet-stream")},
        headers=headers,
    )
    assert resp.status_code == 422
    assert "link" in resp.json()["detail"]
    s3_mail_mock.put_attachment.assert_not_awaited()


async def test_upload_requires_auth(
    client: httpx.AsyncClient, s3_mail_mock: AsyncMock
) -> None:
    resp = await client.post(
        "/email-attachments",
        files={"file": ("a.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 401


async def _upload(db, s3, org_id):
    return await create_email_attachment(
        principal=Principal(
            auth_kind="apikey",
            api_key_id=uuid4(),
            user_id=uuid4(),
            organization_id=org_id,
            scopes=["*"],
        ),
        db=db,
        s3=s3,
        file=UploadFile(BytesIO(b"attachment bytes"), filename="note.txt"),
    )


async def test_metadata_commit_failure_does_not_write_storage(session_factory):
    objects = {}

    async def put(key, payload, content_type):
        objects[key] = payload

    async with session_factory() as db:
        await db.execute(text("""
            CREATE FUNCTION reject_test_upload() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'metadata write failed';
            END;
            $$ LANGUAGE plpgsql
        """))
        await db.execute(text("""
            CREATE TRIGGER reject_test_upload BEFORE INSERT ON email_attachment_uploads
            FOR EACH ROW EXECUTE FUNCTION reject_test_upload()
        """))
        await db.commit()
        try:
            with pytest.raises(DBAPIError, match="metadata write failed"):
                await _upload(
                    db, AsyncMock(put_attachment=AsyncMock(side_effect=put)), uuid4()
                )
        finally:
            await db.rollback()
            await db.execute(text("DROP FUNCTION reject_test_upload() CASCADE"))
            await db.commit()
    assert objects == {}


async def test_uncertain_storage_failure_retains_durable_erasure_reference(
    session_factory,
):
    objects = {}
    org_id = uuid4()

    async def put(key, payload, content_type):
        # A timeout can follow a successful storage write.
        objects[key] = payload
        raise TimeoutError("storage result unknown")

    async with session_factory() as db:
        with pytest.raises(TimeoutError, match="storage result unknown"):
            await _upload(
                db, AsyncMock(put_attachment=AsyncMock(side_effect=put)), org_id
            )
        await db.rollback()

    async with session_factory() as db:
        rows = (await db.scalars(select(EmailAttachmentUpload))).all()
        assert len(rows) == 1
        assert rows[0].organization_id == org_id
        assert rows[0].s3_key in objects
        assert rows[0].first_used_at is None


async def test_closure_during_storage_put_keeps_reference_discoverable(session_factory):
    org_id = uuid4()
    objects = {}

    async def put(key, payload, content_type):
        async with session_factory() as closing_db:
            # The request was admitted before deletion committed its marker.
            closing_db.add(
                OrgClosure(
                    organization_id=org_id,
                    closed_at=datetime.now(timezone.utc),
                    source="organization_delete",
                )
            )
            await closing_db.commit()
            row = await closing_db.scalar(
                select(EmailAttachmentUpload).where(
                    EmailAttachmentUpload.organization_id == org_id
                )
            )
            assert (
                row is not None
            ), "closure must find the reference before bytes are written"
            assert row.s3_key == key
        objects[key] = payload

    async with session_factory() as db:
        result = await _upload(
            db, AsyncMock(put_attachment=AsyncMock(side_effect=put)), org_id
        )

    async with session_factory() as db:
        row = await db.get(EmailAttachmentUpload, result.id)
        assert row is not None
        assert row.s3_key in objects


@pytest.mark.parametrize("put_fails", [False, True])
async def test_cancelled_upload_waits_for_storage_to_settle(session_factory, put_fails):
    started = asyncio.Event()
    finish = asyncio.Event()
    objects = {}

    async def put(key, payload, content_type):
        started.set()
        await finish.wait()
        objects[key] = payload
        if put_fails:
            raise TimeoutError("storage result unknown")

    async with session_factory() as db:
        upload = asyncio.create_task(
            _upload(db, AsyncMock(put_attachment=AsyncMock(side_effect=put)), uuid4())
        )
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            for _ in range(2):
                upload.cancel()
                await asyncio.sleep(0)
                assert (
                    not upload.done()
                ), "admission must remain held during storage I/O"
            finish.set()
            with pytest.raises(asyncio.CancelledError):
                await upload
        finally:
            finish.set()
            await asyncio.gather(upload, return_exceptions=True)

    async with session_factory() as db:
        row = await db.scalar(select(EmailAttachmentUpload))
        assert row is not None
        assert row.s3_key in objects
