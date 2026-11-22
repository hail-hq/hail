"""Background worker: turns uploaded knowledge files into searchable pieces.

``POST /knowledge/documents`` writes a ``knowledge_documents`` row in
``pending``. Each tick this worker claims the oldest one with ``FOR UPDATE
SKIP LOCKED`` (several API processes never take the same row), marks it
``processing`` and:

1. fetches the file from the mail bucket (at most its stored size);
2. checks its first bytes match its declared type;
3. decodes it in a credential-free child process (``decode``, run by
   ``run_isolated`` under CPU, memory and time limits);
4. cuts prose into pieces (``text.chunk_blocks``, tokens counted with the
   model's own tokenizer) or takes one piece per CSV row;
5. embeds the pieces on one CPU thread, off the event loop;
6. in one transaction under the workspace's knowledge lock: checks the
   document is still there and still this claim's, checks the workspace's
   piece limit and the global size cap, writes the pieces and marks it
   ``ready``.

A file that cannot be used (wrong type, no text, over a limit) is
``failed`` with a reason for the customer in ``error``. Anything else (the
bucket, the model, the database) puts the row back to ``pending`` for a
later tick, up to ``MAX_ATTEMPTS`` claims; a ``processing`` row whose claim
is older than ``LEASE`` is taken again (a crashed worker). A document
deleted while it is read is simply skipped at step 6.

Each tick also deletes the knowledge base of closed accounts
(``access.delete_org_knowledge``): documents, uploads never made into a
document, and the limits row. A failure at close is retried here while the
workspace still has uploads or its limits row; a failed final folder sweep
(objects no row points to) is logged and needs the delete run by hand.

Same run_forever/tick/stop shape as ``OutboundForwardWorker``, started from
the API's lifespan when the mail bucket is configured. Nothing here logs
file names or text: only ids, counts and exception types.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import datetime, timedelta, timezone
from uuid import UUID

from botocore.exceptions import BotoCoreError, ClientError
from hailhq.core.file_uploads import matches_declared_type
from hailhq.core.isolated_process import run_isolated
from hailhq.core.knowledge.access import (
    STORAGE_FULL,
    FolderSweepFailed,
    delete_org_knowledge,
    knowledge_limits,
    knowledge_lock,
    storage_full,
)
from hailhq.core.knowledge.decode import MAX_TEXT_BYTES
from hailhq.core.knowledge.embed import Embedder, EmbedderUnavailable, load_embedder
from hailhq.core.knowledge.search import vector_literal
from hailhq.core.knowledge.text import Block, Chunk, chunk_blocks, terms
from hailhq.core.models import (
    FileUpload,
    KnowledgeDocument,
    KnowledgePiece,
    OrganizationKnowledge,
    OrgClosure,
)
from hailhq.core.retention import closed_org_ids
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "DECODE_SECONDS",
    "LEASE",
    "MAX_ATTEMPTS",
    "STALE_AFTER",
    "KnowledgeIngestWorker",
]

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
RETRY_MINUTES = 5
# Bucket errors that never clear: the object is gone or not ours to read.
_GONE = frozenset({"NoSuchKey", "404", "NotFound"})
# A 403 uses a try (the next ones 10 and 15 minutes later): a policy change
# can cause it and clear, and so can a missing object without list access.
_DENIED = frozenset({"AccessDenied", "403"})
# A document still not read this long after it was added fails, whatever
# held it up (an outage that never counted a try).
STALE_AFTER = timedelta(days=1)
_SPENT = "the file could not be processed; delete it and upload it again"
_STALE = "Hail could not read the file in time; delete it and add it again"
LEASE = timedelta(minutes=15)
# Wall-clock limit of one decode. The child also has a 30 s CPU limit.
DECODE_SECONDS = 60.0
# Most pieces one file may give, whatever the workspace limit: it must be
# read well within ``LEASE``. About 280 s per 1,000 pieces with the shipped
# int8 model and 410 s with fp32, plus up to 60 s of decoding.
MAX_DOCUMENT_PIECES = 1500
# Pieces written per INSERT.
_INSERT_BATCH = 100

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class _Refused(Exception):
    """The file cannot be used; the message is for the customer."""


class _Unavailable(Exception):
    """The bucket or the model is down: not the file's fault, so the try
    does not count toward ``MAX_ATTEMPTS``."""


def _claimable(now: datetime):
    # ``RETRY_MINUTES`` times (tries used + 1): 5 minutes after a wait that
    # used no try, 10 and 15 after the first and second tries.
    retry_after = func.make_interval(
        0, 0, 0, 0, 0, RETRY_MINUTES * (KnowledgeDocument.attempts + 1)
    )
    return (
        (KnowledgeDocument.status == "pending")
        & (
            KnowledgeDocument.claimed_at.is_(None)
            | (KnowledgeDocument.claimed_at < now - retry_after)
        )
    ) | (
        (KnowledgeDocument.status == "processing")
        & (KnowledgeDocument.claimed_at < now - LEASE)
    )


class KnowledgeIngestWorker:
    def __init__(
        self,
        *,
        session_factory: SessionFactory,
        s3_factory: Callable[[], S3MailClient],
        embedder: Callable[[], Embedder] = load_embedder,
        poll_interval: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._s3_factory = s3_factory
        self._s3: S3MailClient | None = None
        self._embedder = embedder
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()

    def _get_s3(self) -> S3MailClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # pragma: no cover - defensive; logged + retried
                logger.exception("knowledge ingest tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Delete closed accounts' knowledge, then ingest documents until
        none is waiting. Returns how many documents were finished."""
        await self._forget_closed_orgs()
        await self._fail_spent()
        done = 0
        while not self._stop.is_set():
            claimed = await self._claim()
            if claimed is None:
                break
            await self._ingest(*claimed)
            done += 1
        return done

    async def _forget_closed_orgs(self) -> None:
        # Uploads too: a closed workspace may hold files never made into a
        # document, or only its limits row.
        async with self._session_factory() as db:
            org_ids = list(
                (
                    await db.execute(
                        closed_org_ids(datetime.now(timezone.utc)).where(
                            OrgClosure.organization_id.in_(
                                select(FileUpload.organization_id).where(
                                    FileUpload.purpose == "knowledge_document"
                                )
                            )
                            | OrgClosure.organization_id.in_(
                                select(OrganizationKnowledge.organization_id)
                            )
                        )
                    )
                ).scalars()
            )
        for org_id in org_ids:
            try:
                async with self._session_factory() as db:
                    deleted = await delete_org_knowledge(db, org_id, self._get_s3())
                logger.info(
                    "knowledge: deleted %d document(s) of closed org %s",
                    deleted,
                    org_id,
                )
            except FolderSweepFailed as exc:
                # Rows are gone, so nothing here retries it.
                logger.exception(
                    "knowledge: closed org %s: %d document(s) deleted, but the "
                    "folder sweep failed. Action: run the knowledge delete for "
                    "it by hand (docs/operations/knowledge-base.md)",
                    org_id,
                    exc.documents,
                )
            except Exception as exc:
                code = getattr(getattr(exc, "orig", None), "sqlstate", None)
                if code == "55P03":
                    logger.warning(
                        "knowledge: closed org %s: a knowledge row is busy; "
                        "retrying",
                        org_id,
                    )
                else:
                    logger.exception(
                        "knowledge: could not delete closed org %s; retrying",
                        org_id,
                    )

    async def _fail_spent(self) -> None:
        """Rows that used every try (a crashed worker's lease) or waited past
        ``STALE_AFTER`` fail."""
        async with self._session_factory() as db:
            now = (await db.execute(select(func.now()))).scalar_one()
            for rule, error in (
                (KnowledgeDocument.attempts >= MAX_ATTEMPTS, _SPENT),
                (KnowledgeDocument.created_at < now - STALE_AFTER, _STALE),
            ):
                await db.execute(
                    update(KnowledgeDocument)
                    .where(_claimable(now), rule)
                    .values(
                        status="failed",
                        error=error,
                        claimed_at=None,
                        updated_at=func.now(),
                    )
                )
            await db.commit()

    async def _claim(self) -> tuple[UUID, int] | None:
        async with self._session_factory() as db:
            now = (await db.execute(select(func.now()))).scalar_one()
            row = (
                await db.execute(
                    select(KnowledgeDocument.id, KnowledgeDocument.attempts)
                    .where(_claimable(now), KnowledgeDocument.attempts < MAX_ATTEMPTS)
                    .order_by(KnowledgeDocument.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).one_or_none()
            if row is None:
                await db.rollback()
                return None
            attempt = row.attempts + 1
            await db.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.id == row.id)
                .values(
                    status="processing",
                    attempts=attempt,
                    claimed_at=func.now(),
                    error=None,
                    updated_at=func.now(),
                )
            )
            await db.commit()
            return row.id, attempt

    async def _ingest(self, document_id: UUID, attempt: int) -> None:
        try:
            await self._ingest_claimed(document_id, attempt)
        except _Refused as exc:
            await self._finish(document_id, attempt, "failed", error=str(exc))
            logger.info("knowledge: document %s refused", document_id)
        except asyncio.CancelledError:
            raise
        except _Unavailable as exc:
            logger.warning(
                "knowledge: document %s waits: %s unavailable; will retry",
                document_id,
                exc,
            )
            await self._finish(document_id, attempt, "pending", attempts=attempt - 1)
        except Exception as exc:
            last = attempt >= MAX_ATTEMPTS
            logger.warning(
                "knowledge: document %s attempt %d failed (%s)%s",
                document_id,
                attempt,
                type(exc).__name__,
                "" if last else "; will retry",
            )
            if last:
                await self._finish(document_id, attempt, "failed", error=_SPENT)
            else:
                await self._finish(document_id, attempt, "pending")

    async def _finish(
        self,
        document_id: UUID,
        attempt: int,
        status: str,
        *,
        error: str | None = None,
        attempts: int | None = None,
    ) -> None:
        values: dict = {"status": status, "error": error, "updated_at": func.now()}
        if attempts is not None:
            values["attempts"] = attempts
        async with self._session_factory() as db:
            await db.execute(
                update(KnowledgeDocument)
                .where(
                    KnowledgeDocument.id == document_id,
                    KnowledgeDocument.status == "processing",
                    KnowledgeDocument.attempts == attempt,
                )
                .values(**values)
            )
            await db.commit()

    async def _ingest_claimed(self, document_id: UUID, attempt: int) -> None:
        async with self._session_factory() as db:
            row = (
                await db.execute(
                    select(
                        KnowledgeDocument.organization_id,
                        KnowledgeDocument.content_type,
                        FileUpload.s3_key,
                        FileUpload.size_bytes,
                    )
                    .join(FileUpload, FileUpload.id == KnowledgeDocument.file_upload_id)
                    .where(KnowledgeDocument.id == document_id)
                )
            ).one_or_none()
            if row is None:
                return
            org_id = row.organization_id
            limits = await knowledge_limits(db, org_id)
        if limits is None:
            raise _Refused("the knowledge base is not turned on for this workspace")

        try:
            data = await self._get_s3().fetch_raw(row.s3_key, max_bytes=row.size_bytes)
            if len(data) != row.size_bytes:
                raise ValueError("size mismatch")
        except ValueError:
            raise _Refused("the stored file is not the one uploaded; upload it again")
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if code in _GONE:
                raise _Refused("the stored file is gone; upload it again") from exc
            if code in _DENIED:
                raise
            raise _Unavailable(f"bucket ({code or 'error'})") from exc
        except (BotoCoreError, OSError) as exc:
            raise _Unavailable(f"bucket ({type(exc).__name__})") from exc
        if not data or not matches_declared_type(data, row.content_type):
            raise _Refused(f"the file does not look like {row.content_type}")
        decoded = await self._decode(row.content_type, data)
        del data

        try:
            embedder = await asyncio.to_thread(self._embedder)
        except EmbedderUnavailable as exc:
            raise _Unavailable("embedding model") from exc
        # Cutting 8 MB of text counts tokens many times: off the event loop.
        cap = min(limits.max_pieces, MAX_DOCUMENT_PIECES)
        chunks = await asyncio.to_thread(_chunks, decoded, embedder, cap)
        if not chunks:
            raise _Refused("the file has no text")
        if len(chunks) > cap:
            if cap < limits.max_pieces:
                raise _Refused(
                    f"one file gives at most {cap} pieces; split it into "
                    "smaller files"
                )
            raise _Refused(_too_many(cap))
        vectors = await asyncio.to_thread(
            embedder.embed_passages, [c.text for c in chunks]
        )
        await self._store(document_id, attempt, org_id, chunks, vectors)

    async def _decode(self, content_type: str, data: bytes) -> dict:
        try:
            out = await run_isolated(
                "hailhq.core.knowledge.decode",
                content_type,
                stdin=data,
                timeout_s=DECODE_SECONDS,
                max_output=MAX_TEXT_BYTES * 2 + 1024 * 1024,
            )
        except TimeoutError:
            raise _Refused("reading the file took too long") from None
        except ValueError:
            raise _Refused("the file could not be read") from None
        result = await asyncio.to_thread(json.loads, out)
        if "error" in result:
            raise _Refused(str(result["error"])[:300])
        return result

    async def _store(
        self,
        document_id: UUID,
        attempt: int,
        org_id: UUID,
        chunks: list[Chunk],
        vectors,
    ) -> None:
        # Search words for every piece: before the lock, off the event loop.
        rows = await asyncio.to_thread(
            _piece_rows, document_id, org_id, chunks, vectors
        )
        async with self._session_factory() as db:
            await knowledge_lock(db, org_id)
            doc = (
                await db.execute(
                    select(KnowledgeDocument)
                    .where(
                        KnowledgeDocument.id == document_id,
                        KnowledgeDocument.status == "processing",
                        KnowledgeDocument.attempts == attempt,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if doc is None:
                # Deleted, or taken over by a newer claim, while it was read.
                await db.rollback()
                return
            limits = await knowledge_limits(db, org_id)
            if limits is None:
                await db.rollback()
                raise _Refused("the knowledge base is not turned on for this workspace")
            held = (
                await db.execute(
                    select(func.count())
                    .select_from(KnowledgePiece)
                    .where(
                        KnowledgePiece.organization_id == org_id,
                        KnowledgePiece.document_id != document_id,
                    )
                )
            ).scalar_one()
            if held + len(chunks) > limits.max_pieces:
                await db.rollback()
                raise _Refused(_too_many(limits.max_pieces, held))
            if await storage_full(db):
                await db.rollback()
                raise _Refused(STORAGE_FULL)
            await db.execute(
                delete(KnowledgePiece).where(KnowledgePiece.document_id == document_id)
            )
            for start in range(0, len(rows), _INSERT_BATCH):
                await db.execute(_INSERT_PIECES, rows[start : start + _INSERT_BATCH])
            doc.status = "ready"
            doc.piece_count = len(rows)
            doc.error = None
            doc.claimed_at = None
            doc.updated_at = datetime.now(timezone.utc)
            await db.commit()
        logger.info("knowledge: document %s ready, %d pieces", document_id, len(rows))


_INSERT_PIECES = text("""
    INSERT INTO knowledge_pieces
        (document_id, organization_id, ordinal, text, page, csv_row, embedding, terms)
    VALUES
        (:document_id, :organization_id, :ordinal, :text, :page, :csv_row,
         CAST(:embedding AS vector), array_to_tsvector(CAST(:terms AS text[])))
    """)


def _piece_rows(
    document_id: UUID, org_id: UUID, chunks: list[Chunk], vectors
) -> list[dict]:
    return [
        {
            "document_id": document_id,
            "organization_id": org_id,
            "ordinal": i,
            "text": chunk.text,
            "page": chunk.page,
            "csv_row": chunk.csv_row,
            "embedding": vector_literal(vector),
            "terms": list(dict.fromkeys(terms(chunk.text))) or ["_"],
        }
        for i, (chunk, vector) in enumerate(zip(chunks, vectors, strict=True))
    ]


def _too_many(limit: int, held: int = 0) -> str:
    if held:
        return (
            f"the workspace holds {held} pieces and this file would pass its "
            f"limit of {limit}; delete a document or ask Hail support for more"
        )
    return (
        f"the file gives more than {limit} pieces, the workspace's limit; "
        "split it or ask Hail support for more"
    )


def _chunks(decoded: dict, embedder: Embedder, max_pieces: int) -> list[Chunk]:
    if "rows" in decoded:
        return [
            Chunk(text=r["t"], csv_row=int(r["r"]))
            for r in decoded["rows"][: max_pieces + 1]
        ]
    blocks = [
        Block(text=b["t"], page=b.get("p"), heading=bool(b.get("h")))
        for b in decoded.get("blocks", [])
    ]
    return chunk_blocks(blocks, embedder.count_tokens, max_pieces=max_pieces)
