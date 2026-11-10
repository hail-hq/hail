"""Unit tests for EmailAttachmentGcWorker.tick()."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from hailhq.core.email_attachment_gc import EmailAttachmentGcWorker
from hailhq.core.models import FileUpload
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


async def _make_row(
    session: AsyncSession,
    *,
    created_at,
    first_used_at=None,
    purpose: str = "email_attachment",
    uploaded: bool = True,
    link_expires_at=None,
) -> FileUpload:
    row = FileUpload(
        organization_id=uuid4(),
        purpose=purpose,
        filename="f.pdf",
        content_type="application/pdf",
        size_bytes=10 if uploaded else None,
        s3_key=f"outbound-attachments/x/{uuid4()}",
        link_expires_at=link_expires_at,
        token_hash=None if uploaded else "h",
    )
    session.add(row)
    await session.flush()
    # Backdate created_at directly since the column is server-defaulted.
    row.created_at = created_at
    row.uploaded_at = created_at if uploaded else None
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

    return EmailAttachmentGcWorker(
        session_factory=session_factory, s3_factory=lambda: s3
    )


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
    s3.delete_prefix.assert_awaited_once_with(stale_unused.s3_key)

    remaining_ids = set(
        (await async_session.execute(select(FileUpload.id))).scalars().all()
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
    s3.delete_prefix.side_effect = [None, RuntimeError("s3 down"), None]
    worker = _worker(async_session, s3)

    processed = await worker.tick()

    assert processed == 2
    assert s3.delete_prefix.await_count == 3

    remaining_ids = set(
        (await async_session.execute(select(FileUpload.id))).scalars().all()
    )
    # row1 and row3 (S3 delete succeeded) are fully gone from the DB; row2
    # (S3 delete raised) is left untouched for the next tick to retry.
    assert row1.id not in remaining_ids
    assert row2.id in remaining_ids
    assert row3.id not in remaining_ids


async def _ids(session: AsyncSession) -> set:
    return set((await session.execute(select(FileUpload.id))).scalars().all())


async def test_tick_deletes_expired_links_but_keeps_live_ones(
    async_session: AsyncSession,
) -> None:
    now = datetime.now(timezone.utc)
    expired = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=40),
        uploaded=False,
        link_expires_at=now - timedelta(minutes=20),
    )
    # Past its expiry but inside the grace: a PUT may still be finishing.
    recent = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=20),
        uploaded=False,
        link_expires_at=now - timedelta(minutes=5),
    )
    live = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=5),
        uploaded=False,
        link_expires_at=now + timedelta(minutes=10),
    )
    s3 = AsyncMock()
    assert await _worker(async_session, s3).tick() == 1
    remaining = await _ids(async_session)
    assert expired.id not in remaining
    assert recent.id in remaining
    assert live.id in remaining
    # Cut-off PUTs may have left objects under the link's key.
    s3.delete_prefix.assert_awaited_once_with(expired.s3_key)
    s3.delete.assert_not_awaited()


async def test_tick_keeps_an_expired_link_row_when_s3_fails(
    async_session: AsyncSession,
) -> None:
    """The objects go first; a failed S3 call keeps the row for next tick."""
    now = datetime.now(timezone.utc)
    expired = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=40),
        uploaded=False,
        link_expires_at=now - timedelta(minutes=20),
    )
    s3 = AsyncMock()
    s3.delete_prefix.side_effect = RuntimeError("s3 down")
    assert await _worker(async_session, s3).tick() == 0
    assert expired.id in await _ids(async_session)


async def test_tick_deletes_verification_documents_after_an_hour(
    async_session: AsyncSession,
) -> None:
    now = datetime.now(timezone.utc)
    old = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=61),
        purpose="verification_document",
    )
    fresh = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=30),
        purpose="verification_document",
    )
    s3 = AsyncMock()
    assert await _worker(async_session, s3).tick() == 1
    remaining = await _ids(async_session)
    assert old.id not in remaining
    assert fresh.id in remaining
    s3.delete_prefix.assert_awaited_once_with(old.s3_key)


async def test_tick_never_treats_a_verification_document_as_an_attachment(
    async_session: AsyncSession,
) -> None:
    """An email attachment younger than 24h stays; so does a pending link
    that has not expired, even when created long ago."""
    now = datetime.now(timezone.utc)
    attachment = await _make_row(async_session, created_at=now - timedelta(hours=2))
    s3 = AsyncMock()
    assert await _worker(async_session, s3).tick() == 0
    assert attachment.id in await _ids(async_session)


def test_worker_runs_every_ten_minutes() -> None:
    worker = EmailAttachmentGcWorker(session_factory=None, s3_factory=AsyncMock)
    assert worker._poll_interval == 600.0


async def test_tick_deletes_every_attempt_object_of_an_uploaded_row(
    async_session: AsyncSession,
) -> None:
    """A PUT cut off before its update can leave an object beside the one the
    row points at; the sweep deletes the upload's whole prefix."""
    now = datetime.now(timezone.utc)
    old = await _make_row(
        async_session,
        created_at=now - timedelta(minutes=61),
        purpose="verification_document",
    )
    base = old.s3_key
    old.s3_key = f"{base}.4c1d"
    await async_session.commit()
    s3 = AsyncMock()
    assert await _worker(async_session, s3).tick() == 1
    s3.delete_prefix.assert_awaited_once_with(base)
