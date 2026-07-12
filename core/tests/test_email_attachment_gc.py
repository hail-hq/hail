"""Unit tests for EmailAttachmentGcWorker.tick()."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.email_attachment_gc import EmailAttachmentGcWorker
from hailhq.core.models import EmailAttachmentUpload


async def _make_row(
    session: AsyncSession, *, created_at, first_used_at=None
) -> EmailAttachmentUpload:
    row = EmailAttachmentUpload(
        organization_id=uuid4(),
        filename="f.pdf",
        content_type="application/pdf",
        size_bytes=10,
        s3_key=f"outbound-attachments/x/{uuid4()}",
    )
    session.add(row)
    await session.flush()
    # Backdate created_at directly since the column is server-defaulted.
    row.created_at = created_at
    row.first_used_at = first_used_at
    await session.commit()
    await session.refresh(row)
    return row


@pytest.mark.asyncio
async def test_tick_deletes_only_stale_unused_rows(async_session: AsyncSession) -> None:
    now = datetime.now(timezone.utc)
    stale_unused = await _make_row(async_session, created_at=now - timedelta(hours=25))
    fresh_unused = await _make_row(async_session, created_at=now - timedelta(hours=1))
    stale_used = await _make_row(
        async_session,
        created_at=now - timedelta(hours=48),
        first_used_at=now - timedelta(hours=47),
    )

    def session_factory():
        return async_session  # type: ignore[return-value]

    s3 = AsyncMock()
    worker = EmailAttachmentGcWorker(session_factory=lambda: async_session, s3_factory=lambda: s3)

    processed = await worker.tick()

    assert processed == 1
    s3.delete.assert_awaited_once_with(stale_unused.s3_key)

    remaining_ids = set(
        (await async_session.execute(select(EmailAttachmentUpload.id))).scalars().all()
    )
    assert stale_unused.id not in remaining_ids
    assert fresh_unused.id in remaining_ids
    assert stale_used.id in remaining_ids
