"""Background sweep deleting uploads nobody will use (``file_uploads``).

Three rules, run on every tick:

* An upload link never used, ``LINK_SWEEP_GRACE`` past ``link_expires_at``:
  any object a cut-off PUT left behind (each PUT writes
  ``<s3_key>.<attempt>``), then the row.
* A verification document uploaded more than ``VERIFICATION_UPLOAD_TTL``
  ago (1 hour): ``POST /verifications`` deletes the ones it uses, so these
  are left over. Ticks run every 10 minutes so a document never stays long
  past the hour.
* An email attachment uploaded and never used by a send within
  ``UNUSED_TTL`` (24h). Used ones are reusable and kept.

Same run_forever/tick shape as OutboundForwardWorker
(core/hailhq/core/outbound_worker.py), gated in api/hailhq/api/main.py's
lifespan on the mail bucket being configured.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

from hailhq.core.file_uploads import (
    LINK_SWEEP_GRACE,
    VERIFICATION_UPLOAD_TTL,
    delete_upload_objects,
)
from hailhq.core.models import FileUpload
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

UNUSED_TTL = timedelta(hours=24)
GC_BATCH = 100

SessionFactory = Callable[[], "asynccontextmanager[AsyncSession]"]


class EmailAttachmentGcWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        s3_factory: Callable[[], S3MailClient],
        poll_interval: float = 600.0,
    ) -> None:
        self._session_factory = session_factory
        self._s3_factory = s3_factory
        self._s3: S3MailClient | None = None
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    def _get_s3(self) -> S3MailClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        """Drive ``tick()`` until ``stop()`` is called."""
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover — defensive; logged + retried
                logger.exception("email attachment GC tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Run every sweep once. Returns how many rows were deleted."""
        now = datetime.now(timezone.utc)
        deleted = await self._sweep_links(now)
        deleted += await self._sweep_uploaded(
            (FileUpload.purpose == "verification_document")
            & (FileUpload.uploaded_at < now - VERIFICATION_UPLOAD_TTL)
        )
        deleted += await self._sweep_uploaded(
            (FileUpload.purpose == "email_attachment")
            & FileUpload.first_used_at.is_(None)
            & (FileUpload.uploaded_at < now - UNUSED_TTL)
        )
        return deleted

    async def _sweep_uploaded(self, condition) -> int:
        """Delete uploaded rows matching ``condition`` (S3 object + row).

        Each row's S3 delete and DB row-delete commit together, one row at a
        time — a crash between rows leaves already-processed rows fully gone
        from both S3 and the DB, and untouched rows intact for the next tick
        to retry (see ``OutboundForwardWorker``'s module docstring for the
        same "commit per row" rationale).

        Unlike that worker, GC isn't racing other GC replicas over who
        "claims" a row — a duplicate ``s3.delete`` and a duplicate
        ``DELETE ... WHERE id = x`` are both harmless no-ops — so there's no
        need for ``FOR UPDATE SKIP LOCKED`` or a re-query per row. The batch
        of stale candidates is listed once, up front, in its own short-lived
        session, then processed one row (its own S3 delete + DB delete +
        commit) at a time.
        """
        async with self._session_factory() as session:
            stmt = (
                select(FileUpload.id, FileUpload.s3_key)
                .where(FileUpload.uploaded_at.is_not(None), condition)
                .order_by(FileUpload.uploaded_at.asc())
                .limit(GC_BATCH)
            )
            candidates = (await session.execute(stmt)).all()
        if not candidates:
            return 0

        s3 = self._get_s3()
        deleted = 0
        for row_id, s3_key in candidates:
            try:
                await delete_upload_objects(s3, s3_key)
            except Exception:
                logger.warning(
                    "GC: failed to delete S3 object for upload_id=%s; "
                    "skipping row deletion this tick",
                    row_id,
                    exc_info=True,
                )
                continue
            async with self._session_factory() as session:
                await session.execute(delete(FileUpload).where(FileUpload.id == row_id))
                await session.commit()
            deleted += 1
        return deleted

    async def _sweep_links(self, now: datetime) -> int:
        """Delete upload links never used, ``LINK_SWEEP_GRACE`` past expiry.

        The objects go first, so a failed S3 call keeps the row for the next
        tick. No PUT is still running by then: its body read ends by the
        expiry, and it has ``STORE_TIMEOUT`` (shorter than the grace) to
        write S3 and mark the link used. The row delete is still guarded on
        the link being unused.
        """
        expired = (
            FileUpload.uploaded_at.is_(None),
            FileUpload.link_expires_at < now - LINK_SWEEP_GRACE,
        )
        async with self._session_factory() as session:
            stmt = (
                select(FileUpload.id, FileUpload.s3_key)
                .where(*expired)
                .order_by(FileUpload.link_expires_at.asc())
                .limit(GC_BATCH)
            )
            candidates = (await session.execute(stmt)).all()
        if not candidates:
            return 0

        deleted = 0
        for row_id, s3_key in candidates:
            try:
                await delete_upload_objects(self._get_s3(), s3_key)
            except Exception:
                logger.warning(
                    "GC: failed to delete S3 objects for expired link "
                    "upload_id=%s; keeping the row for the next tick",
                    row_id,
                    exc_info=True,
                )
                continue
            async with self._session_factory() as session:
                result = await session.execute(
                    delete(FileUpload).where(FileUpload.id == row_id, *expired)
                )
                await session.commit()
            deleted += result.rowcount or 0
        return deleted


__all__ = ["UNUSED_TTL", "EmailAttachmentGcWorker"]
