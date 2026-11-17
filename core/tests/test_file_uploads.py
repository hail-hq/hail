"""Shared rules for uploaded files: type check, keys, link tokens."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from hailhq.core import file_uploads
from hailhq.core.db import session_scope
from hailhq.core.file_uploads import (
    hash_token,
    matches_declared_type,
    new_token,
    object_key,
    valid_content_type,
)
from hailhq.core.models import FileUpload
from hailhq.core.s3_mail import S3MailClient


@pytest.mark.parametrize(
    ("content", "content_type", "ok"),
    [
        (b"%PDF-1.4 rest", "application/pdf", True),
        (b"junk" * 10 + b"%PDF-1.7", "application/pdf", True),
        (b"a" * 1024 + b"%PDF-", "application/pdf", False),
        (b"\xff\xd8\xff\xe0 rest", "image/jpeg", True),
        (b"\xff\xd8 rest", "image/jpeg", False),
        (b"\x89PNG\r\n\x1a\n rest", "image/png", True),
        (b"PNG", "image/png", False),
        (b"%PDF-1.4", "image/png", False),
        (b"%PDF-1.4", "text/plain", False),
    ],
)
def test_matches_declared_type(content: bytes, content_type: str, ok: bool) -> None:
    assert matches_declared_type(content, content_type) is ok


def test_object_key_by_purpose() -> None:
    org, upload = uuid4(), uuid4()
    assert (
        object_key("email_attachment", org, upload)
        == f"outbound-attachments/{org}/{upload}"
    )
    assert (
        object_key("verification_document", org, upload)
        == f"verification-uploads/{org}/{upload}"
    )


def test_token_is_url_safe_and_only_its_hash_is_kept() -> None:
    token = new_token()
    assert len(token) >= 43
    assert all(c.isalnum() or c in "-_" for c in token)
    assert hash_token(token) == hashlib.sha256(token.encode()).hexdigest()
    assert new_token() != token


@pytest.mark.parametrize(
    ("value", "ok"),
    [
        ("application/pdf", True),
        ("image/svg+xml", True),
        ("application/vnd.ms-excel", True),
        ("text/plain", True),
        ("text/plain; charset=utf-8", False),
        ("text/plain'; rm -rf ~", False),
        ("nope", False),
        ("", False),
        ("a/" + "b" * 200, False),
    ],
)
def test_valid_content_type(value: str, ok: bool) -> None:
    assert valid_content_type(value) is ok


async def test_delete_org_verification_uploads_deletes_both_prefixes() -> None:
    """The uploads and the copies kept for a resubmit."""
    store = AsyncMock()
    store.delete_prefix.return_value = 2
    org = uuid4()
    assert await file_uploads.delete_org_verification_uploads(org, store) == 4
    assert [c.args for c in store.delete_prefix.await_args_list] == [
        (f"verification-uploads/{org}/",),
        (f"verification-kept/{org}/",),
    ]


def test_kept_files_have_their_own_folder_and_limit() -> None:
    org, upload = uuid4(), uuid4()
    key = file_uploads.object_key(file_uploads.KEPT_PURPOSE, org, upload)
    assert key == f"verification-kept/{org}/{upload}"
    assert file_uploads.max_bytes(file_uploads.KEPT_PURPOSE) == 10 * 1024 * 1024
    # Hail makes kept files itself: no client may upload one.
    assert file_uploads.KEPT_PURPOSE not in file_uploads.PURPOSES


@pytest.mark.parametrize(
    ("name", "shown"),
    [("ada.jpg", "ada.jpg"), ("C:\\scans\\id.pdf", "id.pdf"), ("", "document")],
)
def test_kept_filename(name: str, shown: str) -> None:
    assert file_uploads.kept_filename(name) == shown


async def test_put_attachment_asks_s3_to_encrypt() -> None:
    client = MagicMock()
    store = S3MailClient(client=client, bucket="b")
    await store.put_attachment("k", b"x", "image/png", encrypt=True)
    assert client.put_object.call_args.kwargs["ServerSideEncryption"] == "AES256"
    await store.put_attachment("k", b"x", "image/png")
    assert "ServerSideEncryption" not in client.put_object.call_args.kwargs


def test_object_prefix_drops_the_attempt_suffix() -> None:
    from hailhq.core.file_uploads import object_prefix

    base = "verification-uploads/0b6c2f1e-0000-4000-8000-000000000001/9d7e1c2a-0000-4000-8000-000000000002"
    assert object_prefix(f"{base}.3f9a0c") == base
    # A direct upload has no suffix.
    assert object_prefix(base) == base


def test_object_prefix_of_a_key_with_no_folder() -> None:
    from hailhq.core.file_uploads import object_prefix

    assert object_prefix("id.3f9a0c") == "id"


async def test_store_upload_tracks_bytes_before_storage_write(async_session):
    payload = b"the original bytes"
    row = FileUpload(
        organization_id=uuid4(),
        purpose="email_attachment",
        filename="original.txt",
        content_type="text/plain",
    )
    s3 = AsyncMock()

    async def put(key, content, content_type):
        async with session_scope() as other:
            tracked = await other.get(FileUpload, row.id)
            assert tracked is not None and tracked.first_used_at is None
            assert tracked.sha256 == hashlib.sha256(payload).hexdigest()
        assert content == payload

    s3.put_attachment.side_effect = put
    assert await file_uploads.store_upload(async_session, s3, row, payload) is row
    assert row.s3_key == object_key("email_attachment", row.organization_id, row.id)
    assert row.uploaded_at is not None and row.size_bytes == len(payload)


async def test_failed_storage_write_leaves_a_tracked_unused_upload(async_session):
    row = FileUpload(
        organization_id=uuid4(),
        purpose="email_attachment",
        filename="original.txt",
        content_type="text/plain",
    )
    s3 = AsyncMock()
    s3.put_attachment.side_effect = OSError("storage unavailable")
    with pytest.raises(OSError):
        await file_uploads.store_upload(async_session, s3, row, b"original")
    async with session_scope() as other:
        tracked = await other.get(FileUpload, row.id)
        assert tracked is not None and tracked.first_used_at is None
        assert tracked.uploaded_at is not None
