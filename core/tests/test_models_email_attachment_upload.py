"""Smoke test: EmailAttachmentUpload round-trips through the ORM."""

from __future__ import annotations

from uuid import uuid4

from hailhq.core.models import EmailAttachmentUpload
from sqlalchemy.ext.asyncio import AsyncSession


async def test_create_and_fetch_upload_row(async_session: AsyncSession) -> None:
    org_id = uuid4()
    row = EmailAttachmentUpload(
        organization_id=org_id,
        filename="invoice.pdf",
        content_type="application/pdf",
        size_bytes=1234,
        s3_key=f"outbound-attachments/{org_id}/{uuid4()}",
    )
    async_session.add(row)
    await async_session.commit()
    await async_session.refresh(row)

    assert row.id is not None
    assert row.first_used_at is None
    assert row.created_at is not None
