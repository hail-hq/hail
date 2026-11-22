"""Who may use the knowledge base, and how much.

Nothing is free: the knowledge base is off for every workspace until Hail
adds its ``organization_knowledge`` row (docs/operations/knowledge-base.md).
The row's limits default to early access: 10 documents, 10 MB per file,
1,000 pieces. A workspace with no row gets 403 ``KNOWLEDGE_OFF`` from every
knowledge route, its uploads of ``knowledge_document`` files are refused,
and its agents answer without the knowledge base even when
``agents.knowledge`` is on.

On top of every workspace's own limits, ``GLOBAL_MAX_BYTES`` caps all
knowledge data together, measured as the on-disk size of the two knowledge
tables (with their indexes and TOAST): past it, new uploads and documents
are refused, and the ingest worker refuses to write more pieces.

The caps are checked under ``knowledge_lock`` (an advisory lock per
workspace), so two requests at once never both pass the last free slot.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.db import key_lock, session_scope
from hailhq.core.file_uploads import KNOWLEDGE_DOCUMENTS_PREFIX, delete_upload_objects
from hailhq.core.models import (
    Agent,
    FileUpload,
    KnowledgeDocument,
    OrganizationKnowledge,
)
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "GLOBAL_MAX_BYTES",
    "KNOWLEDGE_OFF",
    "MEMORY_MAX_PIECES",
    "STORAGE_FULL",
    "FolderSweepFailed",
    "agent_uses_knowledge",
    "delete_org_knowledge",
    "involves_database",
    "knowledge_bytes",
    "knowledge_limits",
    "knowledge_lock",
    "log_failure",
    "short_cause",
    "storage_full",
]

logger = logging.getLogger(__name__)
T = TypeVar("T")

GLOBAL_MAX_BYTES = 300 * 1024 * 1024
# A workspace with more pieces than this is searched in SQL during calls
# instead of being loaded into each call's memory (hailhq.voicebot.knowledge).
# 5,000 pieces is about 7.5 MB of vectors plus their text per call.
MEMORY_MAX_PIECES = 5000

KNOWLEDGE_OFF = (
    "the knowledge base is not turned on for this workspace; contact Hail "
    "support to turn it on"
)
STORAGE_FULL = (
    "Hail's knowledge storage is full for now; no new documents can be "
    "added. Contact Hail support"
)


async def knowledge_limits(
    db: AsyncSession, organization_id: UUID
) -> OrganizationKnowledge | None:
    """The workspace's limits, or None when the knowledge base is off."""
    return await db.get(OrganizationKnowledge, organization_id)


async def knowledge_lock(db: AsyncSession, organization_id: UUID) -> None:
    """Serialize cap checks for one workspace until the transaction ends.
    Its own key: never the workspace lock, which carrier calls hold."""
    await key_lock(db, f"knowledge:{organization_id}")


async def knowledge_bytes(db: AsyncSession) -> int:
    """All knowledge data on disk: both tables, their indexes and TOAST."""
    return int(
        (
            await db.execute(
                text(
                    "SELECT pg_total_relation_size('knowledge_pieces') "
                    "+ pg_total_relation_size('knowledge_documents')"
                )
            )
        ).scalar_one()
    )


async def storage_full(db: AsyncSession) -> bool:
    """True past ``GLOBAL_MAX_BYTES``."""
    return await knowledge_bytes(db) >= GLOBAL_MAX_BYTES


async def agent_uses_knowledge(
    db: AsyncSession, organization_id: UUID, agent_id: UUID
) -> bool:
    """The agent has ``knowledge`` on and its workspace has the knowledge
    base turned on. Read from the database, never from call metadata."""
    row = (
        await db.execute(
            select(Agent.knowledge, OrganizationKnowledge.organization_id)
            .outerjoin(
                OrganizationKnowledge,
                OrganizationKnowledge.organization_id == Agent.organization_id,
            )
            .where(Agent.id == agent_id, Agent.organization_id == organization_id)
        )
    ).one_or_none()
    return bool(row is not None and row[0] and row[1] is not None)


def involves_database(exc: BaseException) -> bool:
    """True when ``exc``, any error it was raised from or during, or any
    error in an exception group is a SQLAlchemy error: its traceback could
    print statements and values."""
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, SQLAlchemyError) or type(current).__module__.startswith(
            ("asyncpg", "psycopg")
        ):
            return True
        # An exception group (Python 3.11+) prints every member.
        members = getattr(current, "exceptions", None)
        if isinstance(members, tuple):
            stack.extend(m for m in members if isinstance(m, BaseException))
        stack.extend(e for e in (current.__cause__, current.__context__) if e)
    return False


# The folder holding the ``hailhq`` packages: frames under it are Hail's.
_HAIL_ROOT = str(Path(__file__).resolve().parents[2])


def _where(exc: BaseException) -> str:
    """ " at <file>:<line>" of the deepest frame in Hail's code, else of the
    deepest frame (file name only)."""
    tb, ours, last = exc.__traceback__, None, None
    while tb is not None:
        last = tb
        if tb.tb_frame.f_code.co_filename.startswith(_HAIL_ROOT):
            ours = tb
        tb = tb.tb_next
    frame = ours or last
    if frame is None:
        return ""
    path = frame.tb_frame.f_code.co_filename
    shown = path[len(_HAIL_ROOT) :].lstrip("/") if ours else path.rsplit("/", 1)[-1]
    return f" at {shown}:{frame.tb_lineno}"


def log_failure(
    log: logging.Logger,
    message: str,
    exc: BaseException,
    *,
    level: int = logging.ERROR,
) -> None:
    """Log ``message`` with the failure's shape only: each error in its
    chain (and in an exception group) as type, SQL state and where it was
    raised, never a message. Messages here can hold statements, values or
    text from the customer's file."""
    parts = []
    seen: set[int] = set()
    stack: list[BaseException] = [exc]
    while stack:
        current = stack.pop(0)
        if id(current) in seen:
            continue
        seen.add(id(current))
        code = getattr(current, "sqlstate", None) or getattr(
            getattr(current, "orig", None), "sqlstate", None
        )
        parts.append(
            f"{type(current).__name__}{f' {code}' if code else ''}{_where(current)}"
        )
        members = getattr(current, "exceptions", None)
        if isinstance(members, tuple):
            stack.extend(m for m in members if isinstance(m, BaseException))
        stack.extend(e for e in (current.__cause__, current.__context__) if e)
    log.log(level, "%s (%s)", message, " <- ".join(parts))


def short_cause(exc: BaseException) -> str:
    """ ": <first line of the error, at most 200 characters>", or "" when the
    error has no text."""
    if isinstance(exc, SQLAlchemyError):
        # A database message can quote statements and values: type and code
        # only.
        orig = getattr(exc, "orig", None)
        code = getattr(orig, "sqlstate", None)
        name = type(orig).__name__ if orig is not None else ""
        return (f": {name}" if name else "") + (f" {code}" if code else "")
    if involves_database(exc):
        return ""
    try:
        first = (str(exc).strip().splitlines() or [""])[0][:200]
    except Exception:
        return ""
    return f": {first}" if first else ""


class FolderSweepFailed(RuntimeError):
    """The rows are deleted (``documents`` of them); only the final folder
    sweep failed. Run the delete again to finish it."""

    def __init__(self, documents: int, cause: Exception) -> None:
        why = type(cause).__name__ + short_cause(cause)
        super().__init__(
            f"deleted {documents} document(s), but the folder sweep failed "
            f"({why}); run the delete again (docs/operations/knowledge-base.md)"
        )
        self.documents = documents


async def _end(db: AsyncSession, work: Awaitable[T]) -> T:
    """Run ``work`` and commit; on any error roll back and raise the first
    error. A failed rollback is logged and never replaces it."""
    try:
        result = await work
        await db.commit()
        return result
    except BaseException:
        try:
            await db.rollback()
        except Exception as exc:
            logger.warning("knowledge: rollback failed (%s)", type(exc).__name__)
        raise


async def _turn_off(db: AsyncSession, organization_id: UUID) -> list[tuple[UUID, str]]:
    """Off and links stopped, under the workspace's knowledge lock. Returns
    the knowledge uploads that exist now (id, storage key)."""
    await knowledge_lock(db, organization_id)
    # The upload sweep may hold an upload row during a storage delete: wait
    # a bounded time, then fail (safe to run again) instead of holding the
    # workspace's lock for as long.
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    await db.execute(
        update(FileUpload)
        .where(
            FileUpload.organization_id == organization_id,
            FileUpload.purpose == "knowledge_document",
            FileUpload.token_hash.is_not(None),
        )
        .values(token_hash=None)
    )
    # Its files go in step 2: no upload may become a document from now on
    # (409). If the delete stops, running it again removes these rows.
    await db.execute(
        update(FileUpload)
        .where(
            FileUpload.organization_id == organization_id,
            FileUpload.purpose == "knowledge_document",
            FileUpload.first_used_at.is_(None),
        )
        .values(first_used_at=func.now())
    )
    await db.execute(
        delete(OrganizationKnowledge).where(
            OrganizationKnowledge.organization_id == organization_id
        )
    )
    rows = await db.execute(
        select(FileUpload.id, FileUpload.s3_key).where(
            FileUpload.organization_id == organization_id,
            FileUpload.purpose == "knowledge_document",
        )
    )
    return [(r.id, r.s3_key) for r in rows]


async def _delete_rows(
    db: AsyncSession, organization_id: UUID, upload_ids: list[UUID]
) -> int:
    """The listed uploads (their documents and pieces go with them, ON
    DELETE CASCADE), under the lock; returns the documents deleted."""
    await knowledge_lock(db, organization_id)
    count = (
        await db.execute(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(
                KnowledgeDocument.organization_id == organization_id,
                KnowledgeDocument.file_upload_id.in_(upload_ids),
            )
        )
    ).scalar_one()
    await db.execute(
        delete(FileUpload).where(
            FileUpload.organization_id == organization_id,
            FileUpload.id.in_(upload_ids),
        )
    )
    return int(count)


async def _sweep_folder(
    db: AsyncSession, organization_id: UUID, s3: S3MailClient
) -> None:
    """Empty the workspace's folder (objects no row points to: a cut-off
    PUT, a failed sweep delete), under the knowledge lock and only while
    the workspace is still off, so no new link or document starts
    meanwhile and a workspace turned on again keeps its new files."""
    await knowledge_lock(db, organization_id)
    if await db.get(OrganizationKnowledge, organization_id) is None:
        await s3.delete_prefix(f"{KNOWLEDGE_DOCUMENTS_PREFIX}{organization_id}/")


async def delete_org_knowledge(
    db: AsyncSession,
    organization_id: UUID,
    s3: S3MailClient | None,
) -> int:
    """Delete a workspace's knowledge base: its files from the bucket, its
    upload rows (their documents and pieces go with them) and its limits
    row, which turns it off. The one function for every such delete: today
    an account closing (``org_closures`` route and the ingest worker) and
    Hail by hand (``python -m hailhq.core.knowledge.access delete <org-id>``,
    docs/operations/knowledge-base.md); later a paid plan ending. Returns the
    documents deleted. Safe to run again after any failure.

    1. Under the workspace's knowledge lock: unused upload links stop
       working, uploads not yet documents can no longer become ones (409),
       the limits row is deleted (no new link or document can start), and
       the uploads that exist are listed. Committed. A PUT
       already reading its body then cannot mark its link, so it deletes
       its own object.
    2. The listed uploads' objects (every attempt) are deleted, with no
       lock held. A failure raises with the workspace off and its rows
       kept, so a retry (the closed-account sweep finds the workspace by
       its uploads, or the command run again) finds them.
    3. The listed rows are deleted, under the lock. Committed: the
       customer's text is gone.
    4. Under the lock and only while the workspace is still off, its whole
       folder is emptied (objects no row points to). A failure here raises
       ``FolderSweepFailed`` after the rows are gone; running the delete
       again sweeps the rest.

    If any step fails, run the delete again before turning the workspace
    on. The closed-account sweep retries steps 1 to 3 by itself (it finds
    the workspace by its uploads); a failed step 4 is logged as an action
    for Hail (run the delete by hand).

    With no bucket configured (``s3`` None) no file can exist (every upload
    needs the bucket), so only rows go.
    """
    uploads = await _end(db, _turn_off(db, organization_id))
    if s3 is not None:
        for _, key in uploads:
            await delete_upload_objects(s3, key)
    count = await _end(db, _delete_rows(db, organization_id, [i for i, _ in uploads]))
    if s3 is not None:
        try:
            await _end(db, _sweep_folder(db, organization_id, s3))
        except Exception as exc:
            raise FolderSweepFailed(count, exc) from exc
    return count


async def _delete_main(organization_id: UUID) -> None:
    if not settings.hail_mail_bucket:
        # The files would stay with nothing pointing to them.
        sys.exit("HAIL_MAIL_BUCKET is not set: refusing to delete only the rows")
    store = S3MailClient(bucket=settings.hail_mail_bucket)
    async with session_scope() as db:
        try:
            deleted = await delete_org_knowledge(db, organization_id, store)
        except FolderSweepFailed as exc:
            sys.exit(str(exc))
        except DBAPIError as exc:
            if getattr(exc.orig, "sqlstate", None) != "55P03":
                raise
            sys.exit("a knowledge row is busy for a moment; run the same command again")
    print(f"deleted {deleted} document(s); the knowledge base is off")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "delete":
        sys.exit("usage: python -m hailhq.core.knowledge.access delete <org-id>")
    asyncio.run(_delete_main(UUID(sys.argv[2])))
