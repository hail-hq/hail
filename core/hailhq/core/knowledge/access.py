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

import logging
from uuid import UUID

from hailhq.core.db import key_lock
from hailhq.core.file_uploads import KNOWLEDGE_DOCUMENTS_PREFIX
from hailhq.core.models import (
    Agent,
    FileUpload,
    KnowledgeDocument,
    KnowledgePiece,
    OrganizationKnowledge,
)
from hailhq.core.s3_mail import S3MailClient
from sqlalchemy import delete, func, select, text
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


async def delete_org_knowledge(
    db: AsyncSession,
    organization_id: UUID,
    s3: S3MailClient | None,
) -> int:
    """Delete a workspace's whole knowledge base: its files from the bucket,
    then the upload rows (their documents and pieces go with them), and its
    limits row (so it is off). The one function for every such delete: an
    account closing (``org_closures`` route and the ingest worker), and a
    paid plan ending. Returns the documents deleted. A failed bucket delete
    raises before any row goes, so a retry finds everything again."""
    if s3 is not None:
        await s3.delete_prefix(f"{KNOWLEDGE_DOCUMENTS_PREFIX}{organization_id}/")
    count = (
        await db.execute(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.organization_id == organization_id)
        )
    ).scalar_one()
    await db.execute(
        delete(FileUpload).where(
            FileUpload.organization_id == organization_id,
            FileUpload.purpose == "knowledge_document",
        )
    )
    # Rows whose upload is already gone cannot exist (ON DELETE CASCADE), but
    # pieces are deleted by workspace too, as a guard.
    await db.execute(
        delete(KnowledgePiece).where(KnowledgePiece.organization_id == organization_id)
    )
    await db.execute(
        delete(OrganizationKnowledge).where(
            OrganizationKnowledge.organization_id == organization_id
        )
    )
    await db.commit()
    return int(count)
