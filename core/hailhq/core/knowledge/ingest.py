"""Background worker: turns uploaded knowledge files into searchable pieces.

``POST /knowledge/documents`` writes a ``knowledge_documents`` row in
``pending``. Each tick this worker claims the oldest one with ``FOR UPDATE
SKIP LOCKED`` (several API processes never take the same row), marks it
``processing`` and:

1. fetches the file from the mail bucket (at most its stored size);
2. checks its first bytes match its declared type;
3. decodes it in a credential-free child process (``decode``, run by
   ``run_isolated`` under CPU, memory and time limits);
4. cuts prose into pieces (``text.chunk_blocks``, tokens estimated) or
   takes one piece per CSV row;
5. in one transaction under the workspace's knowledge lock: checks the
   document is still there and still this claim's, checks the workspace's
   piece limit and the global size cap, writes the pieces (no vectors yet)
   and marks it ``ready``. Keyword search finds them from here on.

Then the embedding pass (``_embed_pieces``) sends every piece of a
workspace with the knowledge base on whose vector is missing or was made
by another model than the configured one to the embedding API
(``embed``), ``embed.BATCH`` per request, and stores vector and model id.
The same pass embeds new pieces, retries failed ones, and re-embeds
everything after a model change; meanwhile search uses keywords for those
pieces. An API error or timeout never fails a document: the pass logs a
warning and waits (the server's Retry-After, or 30 s doubling up to 10
minutes) before it tries again.

A batch the API refuses (a 400, 413 or 422) is checked with a short probe
text: a refused probe means the setting (a bad key or width), and the pass
backs off. Otherwise each piece is sent alone, and a piece refused alone
is marked (``embedding_refused`` = the model, with the time) and leaves
the queue, so it never holds up other pieces or other workspaces; it is
tried again after ``REFUSED_RETRY`` or when the model changes. Each batch takes the first pieces of every workspace in
turn, so one workspace's large file never fills every batch.

No transaction stays open during an API request: pieces are read,
embedded, then written only where they still lack a vector of the model.
Before each batch the pass checks the global size cap with a batch of
new vectors counted; while it is reached, pieces with no vector wait
(keyword search) and only vectors of another model are replaced, up to
``REEMBED_HEADROOM`` past the cap, so a model change can finish. The API runs one worker
process; several would at worst embed a piece twice.

A file that cannot be used (wrong type, no text, over a limit) is
``failed`` with a reason for the customer in ``error``. Anything else (the
bucket, the database) puts the row back to ``pending`` for a
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
import time
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
    log_failure,
    storage_full,
)
from hailhq.core.knowledge.decode import MAX_TEXT_BYTES
from hailhq.core.knowledge.embed import (
    BATCH,
    Embedder,
    EmbeddingUnavailable,
    get_embedder,
)
from hailhq.core.knowledge.search import vector_literal
from hailhq.core.knowledge.text import (
    Block,
    Chunk,
    chunk_blocks,
    estimate_tokens,
    terms,
)
from hailhq.core.models import (
    FileUpload,
    KnowledgeDocument,
    KnowledgePiece,
    OrganizationKnowledge,
    OrgClosure,
)
from hailhq.core.retention import closed_org_ids
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, func, or_, select, text, update
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
# Most pieces one file may give, whatever the workspace limit, so one file
# never holds the embedding pass for long.
MAX_DOCUMENT_PIECES = 1500
# Pieces embedded in one tick at most; the rest wait for the next tick.
EMBED_PER_TICK = 1000
# Waits after an embedding API failure: doubling from the first to the last.
EMBED_BACKOFF = (30.0, 600.0)
# A piece refused alone is tried again after this, in case the refusal was
# a passing fault or the setting was fixed under the same model.
REFUSED_RETRY = timedelta(days=1)
# How far past the global cap replacing other-model vectors may go (each
# UPDATE writes a new row and index entry until vacuum).
REEMBED_HEADROOM = 30 * 1024 * 1024
# Sent when a batch is refused: a refusal of this too means the setting.
_PROBE_TEXT = "Opening hours"
# What a piece's vector adds to the knowledge tables: about 3 KB of
# halfvec(1536) and about as much in the HNSW index. Counted when a
# document is admitted, since the embedding pass adds it later.
_VECTOR_BYTES = 6 * 1024
# Pieces written per INSERT.
_INSERT_BATCH = 100

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class _Refused(Exception):
    """The file cannot be used; the message is for the customer."""


class _Unavailable(Exception):
    """The bucket is down: not the file's fault, so the try does not count
    toward ``MAX_ATTEMPTS``."""


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
        embedder: Callable[[], Embedder | None] = get_embedder,
        poll_interval: float = 5.0,
    ) -> None:
        self._session_factory = session_factory
        self._s3_factory = s3_factory
        self._s3: S3MailClient | None = None
        self._embedder = embedder
        self._poll_interval = poll_interval
        self._stop = asyncio.Event()
        # Closed orgs whose delete failed already: logged at error level
        # once, then as a warning on each retry.
        self._reported: set[UUID] = set()
        # The embedding pass waits until this ``time.monotonic()`` after a
        # failure; ``_embed_failures`` failures in a row so far.
        self._embed_after = 0.0
        self._embed_failures = 0
        # The probe text passed in the current pass: not sent again.
        self._probe_ok = False
        # "storage is full" was logged; not again until it clears.
        self._full_warned = False

    def _get_s3(self) -> S3MailClient:
        if self._s3 is None:
            self._s3 = self._s3_factory()
        return self._s3

    async def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception as exc:  # pragma: no cover - defensive; retried
                log_failure(logger, "knowledge ingest tick failed", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._poll_interval)
            except asyncio.TimeoutError:
                pass

    async def stop(self) -> None:
        self._stop.set()

    async def tick(self) -> int:
        """Delete closed accounts' knowledge, ingest documents until none is
        waiting, then embed pieces. Returns how many documents were
        finished."""
        await self._forget_closed_orgs()
        await self._fail_spent()
        done = 0
        while not self._stop.is_set():
            claimed = await self._claim()
            if claimed is None:
                break
            await self._ingest(*claimed)
            done += 1
        await self._embed_pieces()
        return done

    async def _embed_pieces(self) -> int:
        """Embed pieces with no vector of the configured model, document by
        document. Returns how many were embedded."""
        if time.monotonic() < self._embed_after:
            return 0
        embedder = self._embedder()
        if embedder is None:
            return 0
        self._probe_ok = False  # Each pass sends the probe again when needed.
        done = tried = 0
        while not self._stop.is_set() and tried < EMBED_PER_TICK:
            # New vectors (pieces stored with no key) must fit under the cap.
            # Past it, only vectors of another model are replaced, within
            # ``REEMBED_HEADROOM`` more (each UPDATE writes a new row until
            # vacuum), so a model change can finish; past that, it waits.
            async with self._session_factory() as db:
                full = await storage_full(db, adding=BATCH * _VECTOR_BYTES)
                over = full and await storage_full(
                    db, adding=BATCH * _VECTOR_BYTES - REEMBED_HEADROOM
                )
            if full and not self._full_warned:
                logger.warning(
                    "knowledge: storage is full; new vectors wait (keyword "
                    "search), other-model vectors are replaced within %d MB",
                    REEMBED_HEADROOM // (1024 * 1024),
                )
            self._full_warned = full
            if over:
                self._embed_after = time.monotonic() + EMBED_BACKOFF[1]
                break
            # Each workspace's pieces in turn (missing vectors first, then
            # those of another model), so no workspace fills every batch.
            turn = (
                func.row_number()
                .over(
                    partition_by=KnowledgePiece.organization_id,
                    order_by=(
                        # Pieces refused a day ago go last.
                        KnowledgePiece.embedding_refused.is_not(None),
                        KnowledgePiece.embedding_model.is_not(None),
                        KnowledgePiece.document_id,
                        KnowledgePiece.ordinal,
                    ),
                )
                .label("turn")
            )
            queue = (
                select(
                    KnowledgePiece.id,
                    KnowledgePiece.text,
                    KnowledgePiece.embedding_model,
                    turn,
                )
                .where(
                    KnowledgePiece.embedding_model.is_distinct_from(embedder.model),
                    or_(
                        KnowledgePiece.embedding_refused.is_distinct_from(
                            embedder.model
                        ),
                        func.coalesce(
                            KnowledgePiece.embedding_refused_at,
                            text("'-infinity'::timestamptz"),
                        )
                        < func.now() - REFUSED_RETRY,
                    ),
                    *([KnowledgePiece.embedding_model.is_not(None)] if full else []),
                    KnowledgePiece.organization_id.in_(
                        select(OrganizationKnowledge.organization_id)
                    ),
                )
                .subquery()
            )
            query = (
                select(queue.c.id, queue.c.text, queue.c.embedding_model)
                .order_by(queue.c.turn, queue.c.id)
                .limit(BATCH)
            )
            async with self._session_factory() as db:
                rows = (await db.execute(query)).all()
            if not rows:
                if full:
                    self._embed_after = time.monotonic() + EMBED_BACKOFF[1]
                break
            tried += len(rows)
            pairs, refused, failure = await self._embed_rows(embedder, rows)
            if refused:
                async with self._session_factory() as db:
                    await db.execute(
                        update(KnowledgePiece)
                        .where(
                            KnowledgePiece.id.in_([r.id for r in refused]),
                            KnowledgePiece.embedding_model.is_distinct_from(
                                embedder.model
                            ),
                        )
                        .values(
                            embedding_refused=embedder.model,
                            embedding_refused_at=func.now(),
                        )
                    )
                    await db.commit()
                for row in refused:
                    logger.warning(
                        "knowledge: the embedding API refused piece %s; it keeps "
                        "keyword search",
                        row.id,
                    )
            if pairs:
                async with self._session_factory() as db:
                    await db.execute(
                        _SET_EMBEDDING,
                        [
                            {
                                "id": r.id,
                                "embedding": vector_literal(v),
                                "model": embedder.model,
                            }
                            for r, v in pairs
                        ],
                    )
                    await db.commit()
                done += len(pairs)
            if failure is not None:
                self._back_off(failure, len(rows) - len(pairs) - len(refused))
                break
            self._embed_failures = 0
        if done:
            logger.info("knowledge: embedded %d piece(s)", done)
        return done

    async def _embed_rows(
        self, embedder: Embedder, rows
    ) -> tuple[list, list, EmbeddingUnavailable | None]:
        """The (row, vector) pairs for ``rows``, the rows the API refused on
        their own, and the failure that stopped it, if any. A refused batch
        is checked with a probe text (refused too: the setting, returned as
        the failure), then sent again one piece at a time. Vectors already
        made are kept when a later request fails."""
        try:
            vectors = await embedder.embed([r.text for r in rows])
            return list(zip(rows, vectors, strict=True)), [], None
        except EmbeddingUnavailable as exc:
            if not exc.refused:
                return [], [], exc
        if not self._probe_ok:
            try:
                await embedder.embed([_PROBE_TEXT])
            except EmbeddingUnavailable as exc:
                return [], [], exc
            self._probe_ok = True
        pairs: list = []
        refused: list = []
        for row in rows:
            try:
                pairs.append((row, (await embedder.embed([row.text]))[0]))
            except EmbeddingUnavailable as exc:
                if not exc.refused:
                    return pairs, refused, exc
                refused.append(row)
        return pairs, refused, None

    def _back_off(self, exc: EmbeddingUnavailable, pieces: int) -> None:
        self._embed_failures += 1
        low, high = EMBED_BACKOFF
        doubling = low * 2 ** min(self._embed_failures - 1, 10)
        server = exc.retry_after if exc.retry_after and exc.retry_after > 0 else None
        wait = min(high, server or doubling)
        self._embed_after = time.monotonic() + wait
        logger.warning(
            "knowledge: embedding API failed (%s); %d piece(s) keep keyword "
            "search, next try in %.0f s",
            exc,
            pieces,
            wait,
        )

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
                # Rows are gone, so nothing here retries it. No traceback:
                # its cause may be a database error carrying parameters.
                logger.error(
                    "knowledge: closed org %s: %s. Action: run the knowledge "
                    "delete for it by hand",
                    org_id,
                    exc,
                )
            except Exception as exc:
                code = getattr(getattr(exc, "orig", None), "sqlstate", None)
                if code == "55P03":
                    logger.warning(
                        "knowledge: closed org %s: a knowledge row is busy; "
                        "retrying",
                        org_id,
                    )
                    continue
                # Error level the first time, a warning on later ticks; the
                # chain's shape only (types and where they were raised).
                first = org_id not in self._reported
                self._reported.add(org_id)
                log_failure(
                    logger,
                    f"knowledge: could not delete closed org {org_id}; retrying",
                    exc,
                    level=logging.ERROR if first else logging.WARNING,
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

        # Cutting 8 MB of text counts tokens many times: off the event loop.
        cap = min(limits.max_pieces, MAX_DOCUMENT_PIECES)
        chunks = await asyncio.to_thread(_chunks, decoded, cap)
        if not chunks:
            raise _Refused("the file has no text")
        if len(chunks) > cap:
            if cap < limits.max_pieces:
                raise _Refused(
                    f"one file gives at most {cap} pieces; split it into "
                    "smaller files"
                )
            raise _Refused(_too_many(cap))
        await self._store(document_id, attempt, org_id, chunks)

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
    ) -> None:
        # Search words for every piece: before the lock, off the event loop.
        rows = await asyncio.to_thread(_piece_rows, document_id, org_id, chunks)
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
            # The vectors the embedding pass adds later count too, whether
            # or not an embedding API is set up yet.
            if await storage_full(db, adding=len(rows) * _VECTOR_BYTES):
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
        (document_id, organization_id, ordinal, text, page, csv_row, terms)
    VALUES
        (:document_id, :organization_id, :ordinal, :text, :page, :csv_row,
         array_to_tsvector(CAST(:terms AS text[])))
    """)

# Only where the piece still lacks a vector of this model (another worker
# may have written it meanwhile); a deleted piece updates nothing.
_SET_EMBEDDING = text("""
    UPDATE knowledge_pieces
    SET embedding = CAST(:embedding AS halfvec), embedding_model = :model,
        embedding_refused = NULL, embedding_refused_at = NULL
    WHERE id = :id AND embedding_model IS DISTINCT FROM :model
    """)


def _piece_rows(document_id: UUID, org_id: UUID, chunks: list[Chunk]) -> list[dict]:
    return [
        {
            "document_id": document_id,
            "organization_id": org_id,
            "ordinal": i,
            "text": chunk.text,
            "page": chunk.page,
            "csv_row": chunk.csv_row,
            "terms": list(dict.fromkeys(terms(chunk.text))) or ["_"],
        }
        for i, chunk in enumerate(chunks)
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


def _chunks(decoded: dict, max_pieces: int) -> list[Chunk]:
    if "rows" in decoded:
        return [
            Chunk(text=r["t"], csv_row=int(r["r"]))
            for r in decoded["rows"][: max_pieces + 1]
        ]
    blocks = [
        Block(text=b["t"], page=b.get("p"), heading=bool(b.get("h")))
        for b in decoded.get("blocks", [])
    ]
    return chunk_blocks(blocks, estimate_tokens, max_pieces=max_pieces)
