"""Unit tests for EmailAttachmentGcWorker.tick()."""

from __future__ import annotations

from contextlib import asynccontextmanager
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


def _worker(async_session: AsyncSession, s3) -> EmailAttachmentGcWorker:
    """Build a worker whose ``session_factory`` yields the shared test
    session on every call — mirroring ``OutboundForwardWorker``'s test
    helper (``test_outbound_worker.py::_worker``) so ``tick()`` can open a
    fresh ``async with self._session_factory() as session:`` block per row
    without the underlying session actually being torn down between rows.
    """

    @asynccontextmanager
    async def session_factory():
        yield async_session

    return EmailAttachmentGcWorker(session_factory=session_factory, s3_factory=lambda: s3)


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

    s3 = AsyncMock()
    worker = _worker(async_session, s3)

    processed = await worker.tick()

    assert processed == 1
    s3.delete.assert_awaited_once_with(stale_unused.s3_key)

    remaining_ids = set(
        (await async_session.execute(select(EmailAttachmentUpload.id))).scalars().all()
    )
    assert stale_unused.id not in remaining_ids
    assert fresh_unused.id in remaining_ids
    assert stale_used.id in remaining_ids


@pytest.mark.asyncio
async def test_tick_commits_each_row_independently_of_mid_batch_failure(
    async_session: AsyncSession,
) -> None:
    """A failing S3 delete on one row in the batch must not roll back or
    block the per-row commits for rows processed before or after it — each
    row's S3 delete + DB delete + commit happens on its own, so a crash (or,
    here, a permanent S3 error) affecting one row leaves the others'
    already-committed state untouched."""
    now = datetime.now(timezone.utc)
    row1 = await _make_row(async_session, created_at=now - timedelta(hours=30))
    row2 = await _make_row(async_session, created_at=now - timedelta(hours=29))
    row3 = await _make_row(async_session, created_at=now - timedelta(hours=28))

    s3 = AsyncMock()
    s3.delete.side_effect = [None, RuntimeError("s3 down"), None]
    worker = _worker(async_session, s3)

    processed = await worker.tick()

    assert processed == 2
    assert s3.delete.await_count == 3

    remaining_ids = set(
        (await async_session.execute(select(EmailAttachmentUpload.id))).scalars().all()
    )
    # row1 and row3 (S3 delete succeeded) are fully gone from the DB; row2
    # (S3 delete raised) is left untouched for the next tick to retry.
    assert row1.id not in remaining_ids
    assert row2.id in remaining_ids
    assert row3.id not in remaining_ids
