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
from typing import TypeVar
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.db import key_lock, session_scope
from hailhq.core.file_uploads import KNOWLEDGE_DOCUMENTS_PREFIX
from hailhq.core.models import (
    Agent,
    FileUpload,
    KnowledgeDocument,
    OrganizationKnowledge,
)
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "GLOBAL_MAX_BYTES",
    "KNOWLEDGE_OFF",
    "MEMORY_MAX_PIECES",
    "STORAGE_FULL",
    "agent_uses_knowledge",
    "delete_org_knowledge",
    "knowledge_bytes",
    "knowledge_limits",
    "knowledge_lock",
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
    await db.execute(
        update(FileUpload)
        .where(
            FileUpload.organization_id == organization_id,
            FileUpload.purpose == "knowledge_document",
            FileUpload.token_hash.is_not(None),
        )
        .values(token_hash=None)
    )
    await db.execute(
        delete(OrganizationKnowledge).where(
            OrganizationKnowledge.organization_id == organization_id
        )
    )


async def _delete_rows(db: AsyncSession, organization_id: UUID) -> int:
    """Every knowledge upload of the workspace (their documents and pieces go
    with them, ON DELETE CASCADE), under the lock: the folder is empty now,
    so no row may stay pointing at it. Returns the documents deleted."""
    await knowledge_lock(db, organization_id)
    uploads = (
        FileUpload.organization_id == organization_id,
        FileUpload.purpose == "knowledge_document",
    )
    count = (
        await db.execute(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.organization_id == organization_id)
        )
    ).scalar_one()
    await db.execute(delete(FileUpload).where(*uploads))
    return int(count)


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

    Two short transactions around the storage deletes, so no lock is held
    during storage calls:

    1. Under the workspace's knowledge lock: unused upload links stop
       working and the limits row is deleted (no new link or document can
       start). Committed. A PUT
       already reading its body then cannot mark its link, so it deletes
       its own object.
    2. The workspace's folder is emptied. A failure raises with the
       workspace off and its rows kept, so a retry (the closed-account
       sweep finds the workspace by its uploads, or the command run again)
       finds them.
    3. Every knowledge upload row of the workspace is deleted, so none
       points at the emptied folder.

    Turning the workspace on again while this runs loses what is added
    meanwhile too; do not. With no bucket configured (``s3`` None) no file can
    exist (every upload needs the bucket), so only rows go.
    """
    await _end(db, _turn_off(db, organization_id))
    if s3 is not None:
        # The whole folder: also objects whose rows are already gone (a
        # cut-off PUT, a failed sweep delete).
        await s3.delete_prefix(f"{KNOWLEDGE_DOCUMENTS_PREFIX}{organization_id}/")
    return await _end(db, _delete_rows(db, organization_id))


async def _delete_main(organization_id: UUID) -> None:
    if not settings.hail_mail_bucket:
        # The files would stay with nothing pointing to them.
        sys.exit("HAIL_MAIL_BUCKET is not set: refusing to delete only the rows")
    store = S3MailClient(bucket=settings.hail_mail_bucket)
    async with session_scope() as db:
        deleted = await delete_org_knowledge(db, organization_id, store)
    print(f"deleted {deleted} document(s); the knowledge base is off")


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "delete":
        sys.exit("usage: python -m hailhq.core.knowledge.access delete <org-id>")
    asyncio.run(_delete_main(UUID(sys.argv[2])))
