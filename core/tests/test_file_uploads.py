"""Shared rules for uploaded files: type check, keys, link tokens."""

from __future__ import annotations

import hashlib
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.core import file_uploads
from hailhq.core.file_uploads import (
    hash_token,
    matches_declared_type,
    new_token,
    object_key,
    valid_content_type,
)


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


async def test_delete_org_verification_uploads_deletes_the_prefix() -> None:
    store = AsyncMock()
    store.delete_prefix.return_value = 2
    org = uuid4()
    assert await file_uploads.delete_org_verification_uploads(org, store) == 2
    store.delete_prefix.assert_awaited_once_with(f"verification-uploads/{org}/")


def test_object_prefix_drops_the_attempt_suffix() -> None:
    from hailhq.core.file_uploads import object_prefix

    base = "verification-uploads/0b6c2f1e-0000-4000-8000-000000000001/9d7e1c2a-0000-4000-8000-000000000002"
    assert object_prefix(f"{base}.3f9a0c") == base
    # A direct upload has no suffix.
    assert object_prefix(base) == base


def test_object_prefix_of_a_key_with_no_folder() -> None:
    from hailhq.core.file_uploads import object_prefix

    assert object_prefix("id.3f9a0c") == "id"


async def test_delete_upload_objects_falls_back_to_the_one_key() -> None:
    """A bucket policy without s3:ListBucket: the row's key is still deleted."""
    from unittest.mock import AsyncMock

    from hailhq.core.file_uploads import delete_upload_objects

    s3 = AsyncMock()
    s3.delete_prefix.side_effect = RuntimeError("AccessDenied")
    await delete_upload_objects(s3, "p/o/id.a1")
    s3.delete_prefix.assert_awaited_once_with("p/o/id")
    s3.delete.assert_awaited_once_with("p/o/id.a1")


async def test_delete_upload_objects_raises_when_both_fail() -> None:
    from unittest.mock import AsyncMock

    import pytest
    from hailhq.core.file_uploads import delete_upload_objects

    s3 = AsyncMock()
    s3.delete_prefix.side_effect = RuntimeError("AccessDenied")
    s3.delete.side_effect = RuntimeError("s3 down")
    with pytest.raises(RuntimeError):
        await delete_upload_objects(s3, "p/o/id.a1")
