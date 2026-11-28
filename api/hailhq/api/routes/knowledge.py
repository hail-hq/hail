"""The knowledge base: documents an organization's agents answer from.

Add a file with an upload link (``POST /uploads`` with purpose
``knowledge_document``, then the PUT), then ``POST /knowledge/documents``
with its id. The ingest worker (``hailhq.core.knowledge.ingest``) reads it
in the background: the document goes ``pending``, ``processing``, then
``ready`` or ``failed``. ``DELETE`` removes the file and its pieces.
``POST /knowledge/search`` runs the same hybrid search the text and email
agents use.

Off until Hail turns it on for the workspace: every route answers 403 with
``KNOWLEDGE_OFF`` until then (``hailhq.core.knowledge.access``). The
workspace's document limit and Hail's global storage cap are checked under
the workspace's knowledge lock, so two requests never both take the last
slot. Every query filters on the caller's organization.
"""

from __future__ import annotations

import logging
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from fastapi import status as http_status
from hailhq.api.audit import actor_of, write_audit_log
from hailhq.api.deps import Principal, get_current_principal, get_s3_mail_optional
from hailhq.api.pagination import fetch_cursor_page
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
from hailhq.core.db import get_session
from hailhq.core.file_uploads import KNOWLEDGE_MAX_FILE_BYTES, delete_upload_objects
from hailhq.core.knowledge.access import (
    KNOWLEDGE_OFF,
    STORAGE_FULL,
    WORKSPACE_FULL,
    knowledge_limits,
    knowledge_lock,
    storage_full,
    workspace_full,
)
from hailhq.core.knowledge.embed import EmbeddingUnavailable, get_embedder
from hailhq.core.knowledge.search import search_pieces
from hailhq.core.models import FileUpload, KnowledgeDocument, OrganizationKnowledge
from hailhq.core.s3_mail import S3MailClient
from hailhq.core.schemas import (
    KnowledgeDocumentCreate,
    KnowledgeDocumentListResponse,
    KnowledgeDocumentResponse,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSearchResult,
)
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/knowledge",
    tags=["knowledge"],
    responses={
        **GENERAL_RATE_LIMITED_RESPONSES,
    },
)

_OFF = {
    403: {
        "description": (
            "The knowledge base is not turned on for this workspace. Contact "
            "Hail support."
        )
    }
}

_NOT_FOUND = "knowledge document not found"
_DEFAULT_LIMIT = 50
_MAX_LIMIT = 100


async def knowledge_on(db: AsyncSession, org: UUID) -> OrganizationKnowledge:
    """The workspace's knowledge limits; 403 when the knowledge base is off."""
    limits = await knowledge_limits(db, org)
    if limits is None:
        raise HTTPException(
            status_code=http_status.HTTP_403_FORBIDDEN, detail=KNOWLEDGE_OFF
        )
    return limits


async def _owned(db: AsyncSession, document_id: UUID, org: UUID) -> KnowledgeDocument:
    doc = (
        await db.execute(
            select(KnowledgeDocument).where(
                KnowledgeDocument.id == document_id,
                KnowledgeDocument.organization_id == org,
            )
        )
    ).scalar_one_or_none()
    if doc is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND, detail=_NOT_FOUND
        )
    return doc


async def lock_upload(db: AsyncSession, *where) -> FileUpload | None:
    """The upload row matching ``where``, locked until commit, or None.

    A bounded wait on the upload's row only: the upload sweep may hold it
    while it deletes from storage; the request then answers 503 at once
    instead of holding the workspace's knowledge lock that long."""
    await db.execute(text("SET LOCAL lock_timeout = '5s'"))
    try:
        upload = (
            await db.execute(select(FileUpload).where(*where).with_for_update())
        ).scalar_one_or_none()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        await db.rollback()
        raise HTTPException(
            status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="this upload is busy; try again in a few seconds",
            headers={"Retry-After": "5"},
        ) from exc
    await db.execute(text("SET LOCAL lock_timeout TO DEFAULT"))
    return upload


async def ensure_room(
    db: AsyncSession, org: UUID, limits: OrganizationKnowledge, size_bytes: int
) -> int:
    """Refuse a new file of ``size_bytes`` the workspace has no room for;
    returns its file limit. 413 over the file limit; 409 when it holds its
    limit of documents, its plan's storage, or Hail's knowledge storage is full. The one place
    for every limit a new document is checked against: ``add_document``
    and ``POST /knowledge/uploads``. Under the workspace's knowledge lock."""
    limit = min(KNOWLEDGE_MAX_FILE_BYTES, limits.max_file_bytes)
    if size_bytes > limit:
        raise HTTPException(
            status_code=http_status.HTTP_413_CONTENT_TOO_LARGE,
            detail=f"the file is larger than {limit} bytes",
        )
    held = (
        await db.execute(
            select(func.count())
            .select_from(KnowledgeDocument)
            .where(KnowledgeDocument.organization_id == org)
        )
    ).scalar_one()
    if held >= limits.max_documents:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail=(
                f"the workspace holds its limit of {limits.max_documents} "
                "documents; delete one or ask Hail support for more"
            ),
        )
    if await storage_full(db):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=STORAGE_FULL
        )
    if await workspace_full(db, limits):
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT, detail=WORKSPACE_FULL
        )
    return limit


async def add_document(
    db: AsyncSession,
    principal: Principal,
    upload: FileUpload,
    limits: OrganizationKnowledge,
    name: str | None,
) -> KnowledgeDocumentResponse:
    """Turn a stored upload into a pending document (the ingest worker reads
    it) and commit. The caller holds the workspace's knowledge lock and the
    upload's row lock. 409 when the upload is already a document, the
    workspace holds its limit of documents, or storage is full; 413 over
    the file limit. Used by ``POST /knowledge/documents`` and by a direct
    upload's ``/complete`` (``knowledge_uploads``)."""
    org = principal.organization_id
    if upload.first_used_at is not None:
        raise HTTPException(
            status_code=http_status.HTTP_409_CONFLICT,
            detail="this upload is already a knowledge document",
        )
    await ensure_room(db, org, limits, upload.size_bytes or 0)
    doc = KnowledgeDocument(
        organization_id=org,
        file_upload_id=upload.id,
        name=name or upload.filename,
        content_type=upload.content_type,
        size_bytes=upload.size_bytes or 0,
    )
    upload.first_used_at = func.now()
    db.add(doc)
    await db.commit()
    await db.refresh(doc)
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=org,
        api_key_id=principal.api_key_id,
        action="knowledge_document.create",
        resource_type="knowledge_document",
        resource_id=doc.id,
        payload={"content_type": doc.content_type, "size_bytes": doc.size_bytes},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return KnowledgeDocumentResponse.model_validate(doc)


@router.post(
    "/documents",
    response_model=KnowledgeDocumentResponse,
    status_code=http_status.HTTP_201_CREATED,
    operation_id="create_knowledge_document",
    responses={
        **_OFF,
        404: {"description": "No stored `knowledge_document` upload with this id."},
        409: {
            "description": (
                "The upload is already a document, the workspace holds its "
                "limit of documents, or Hail's knowledge storage is full."
            )
        },
        413: {"description": "The file is larger than the workspace's file limit."},
        503: {
            "description": (
                "Another request or the upload sweep holds this upload; try "
                "again in a few seconds."
            )
        },
    },
)
async def create_knowledge_document(
    body: KnowledgeDocumentCreate,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeDocumentResponse:
    """Add an uploaded file to the knowledge base.

    The file must be a `knowledge_document` upload (`POST /v1/uploads`)
    whose bytes were sent. Hail reads it in the background: poll
    `GET /v1/knowledge/documents/{id}` until `status` is `ready` (agents
    answer from it) or `failed` (`error` says why). PDF (text only, no
    scans), DOCX, TXT, Markdown and CSV (one piece per row) are read.
    """
    org = principal.organization_id
    await knowledge_lock(db, org)
    limits = await knowledge_on(db, org)
    upload = await lock_upload(
        db,
        FileUpload.id == body.upload_id,
        FileUpload.organization_id == org,
        FileUpload.purpose == "knowledge_document",
        FileUpload.uploaded_at.is_not(None),
    )
    if upload is None:
        raise HTTPException(
            status_code=http_status.HTTP_404_NOT_FOUND,
            detail="no stored knowledge_document upload with this id",
        )
    return await add_document(db, principal, upload, limits, body.name)


@router.get(
    "/documents",
    response_model=KnowledgeDocumentListResponse,
    operation_id="list_knowledge_documents",
)
async def list_knowledge_documents(
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    cursor: str | None = Query(default=None),
    limit: int = Query(default=_DEFAULT_LIMIT, ge=1, le=_MAX_LIMIT),
) -> KnowledgeDocumentListResponse:
    """List the workspace's knowledge documents, newest first. Works while
    the knowledge base is off too, so a workspace can see and remove its
    files."""
    rows, next_cursor = await fetch_cursor_page(
        db,
        select(KnowledgeDocument).where(
            KnowledgeDocument.organization_id == principal.organization_id
        ),
        KnowledgeDocument.created_at,
        KnowledgeDocument.id,
        cursor=cursor,
        limit=limit,
        newest_first=True,
    )
    return KnowledgeDocumentListResponse(
        items=[KnowledgeDocumentResponse.model_validate(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.get(
    "/documents/{document_id}",
    response_model=KnowledgeDocumentResponse,
    operation_id="get_knowledge_document",
    responses={404: {"description": "No such document in this workspace."}},
)
async def get_knowledge_document(
    document_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeDocumentResponse:
    """One knowledge document and its reading status. Works while the
    knowledge base is off too."""
    doc = await _owned(db, document_id, principal.organization_id)
    return KnowledgeDocumentResponse.model_validate(doc)


@router.delete(
    "/documents/{document_id}",
    status_code=http_status.HTTP_204_NO_CONTENT,
    operation_id="delete_knowledge_document",
    responses={
        404: {"description": "No such document in this workspace."},
        503: {"description": "The file could not be deleted from storage; try again."},
    },
)
async def delete_knowledge_document(
    document_id: UUID,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
    s3: Annotated[S3MailClient | None, Depends(get_s3_mail_optional)],
) -> Response:
    """Delete a document: its file in storage, then its pieces. Agents stop
    using it for new calls and messages at once. Works while the knowledge
    base is off too, so a workspace can always remove its files."""
    org = principal.organization_id
    doc = await _owned(db, document_id, org)
    upload_id = doc.file_upload_id
    s3_key = (
        await db.execute(select(FileUpload.s3_key).where(FileUpload.id == upload_id))
    ).scalar_one_or_none()
    await db.rollback()
    if s3_key is not None and s3 is not None:
        try:
            await delete_upload_objects(s3, s3_key)
        except Exception as exc:
            raise HTTPException(
                status_code=http_status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="the file could not be deleted from storage; try again",
            ) from exc
    # The upload row cascades to the document and its pieces.
    await db.execute(
        delete(FileUpload).where(
            FileUpload.id == upload_id, FileUpload.organization_id == org
        )
    )
    await db.commit()
    actor_user_id, actor_kind = actor_of(principal)
    await write_audit_log(
        organization_id=org,
        api_key_id=principal.api_key_id,
        action="knowledge_document.delete",
        resource_type="knowledge_document",
        resource_id=document_id,
        payload={},
        actor_user_id=actor_user_id,
        actor_kind=actor_kind,
    )
    return Response(status_code=http_status.HTTP_204_NO_CONTENT)


@router.post(
    "/search",
    response_model=KnowledgeSearchResponse,
    operation_id="search_knowledge",
    responses=_OFF,
)
async def search_knowledge(
    body: KnowledgeSearchRequest,
    principal: Annotated[Principal, Depends(get_current_principal)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> KnowledgeSearchResponse:
    """Search the workspace's ready documents, the way its agents do: by
    meaning (a multilingual model, so a question in one language finds a
    document in another) and by words (IDs and part numbers match however
    they are spaced or dashed), merged by rank. Returns the best pieces."""
    org = principal.organization_id
    await knowledge_on(db, org)
    vector = None
    embedder = get_embedder()
    if embedder is not None:
        try:
            vector = await embedder.embed_query(body.query)
        except Exception as exc:
            # Any failure: keyword search alone, never a 500.
            logger.warning(
                "knowledge search: embedding API failed (%s)",
                exc if isinstance(exc, EmbeddingUnavailable) else type(exc).__name__,
            )
    hits = await search_pieces(
        db,
        org,
        body.query,
        vector,
        body.limit,
        model=embedder.model if embedder is not None else None,
    )
    return KnowledgeSearchResponse(
        results=[
            KnowledgeSearchResult(
                document_id=h.piece.document_id,
                document_name=h.piece.document_name,
                text=h.piece.text,
                page=h.piece.page,
                row=h.piece.csv_row,
                score=round(h.score, 6),
            )
            for h in hits
        ]
    )


__all__ = ["add_document", "ensure_room", "knowledge_on", "lock_upload", "router"]
