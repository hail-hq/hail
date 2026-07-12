"""Integration tests for POST /email-attachments."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from hailhq.api.main import app
from hailhq.api.routes import email_attachments

from .conftest import insert_org_and_key  # noqa: F401


@pytest.fixture()
def s3_mail_mock():
    s3 = AsyncMock()
    app.dependency_overrides[email_attachments._get_s3_mail] = lambda: s3
    try:
        yield s3
    finally:
        app.dependency_overrides.pop(email_attachments._get_s3_mail, None)


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
    oversize = b"x" * (10 * 1024 * 1024 + 1)

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
