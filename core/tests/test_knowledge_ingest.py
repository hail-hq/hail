"""The knowledge ingest worker and the SQL hybrid search, on a real
Postgres with pgvector."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import numpy as np
import pytest
from hailhq.core.file_uploads import object_key
from hailhq.core.knowledge import index as index_module
from hailhq.core.knowledge import search as search_module
from hailhq.core.knowledge.access import agent_uses_knowledge, delete_org_knowledge
from hailhq.core.knowledge.index import MemoryIndex, Piece
from hailhq.core.knowledge.ingest import MAX_ATTEMPTS, KnowledgeIngestWorker
from hailhq.core.knowledge.search import search_pieces
from hailhq.core.models import (
    Agent,
    FileUpload,
    KnowledgeDocument,
    KnowledgePiece,
    OrganizationKnowledge,
    OrgClosure,
)
from hailhq.core.testing.knowledge import NORTHGATE_MANUAL, FakeEmbedder
from sqlalchemy import func, select


class FakeS3:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.fail = False

    async def fetch_raw(self, key: str, *, max_bytes: int | None = None) -> bytes:
        if self.fail:
            raise ConnectionError("bucket down")
        return self.objects[key][: max_bytes or None]

    async def delete_prefix(self, prefix: str, *, keep: str | None = None) -> int:
        gone = [k for k in self.objects if k.startswith(prefix) and k != keep]
        for key in gone:
            del self.objects[key]
        return len(gone)


@pytest.fixture()
def s3() -> FakeS3:
    return FakeS3()


@pytest.fixture()
def worker(session_factory, s3) -> KnowledgeIngestWorker:
    @asynccontextmanager
    async def scope():
        async with session_factory() as session:
            yield session

    return KnowledgeIngestWorker(
        session_factory=scope, s3_factory=lambda: s3, embedder=FakeEmbedder
    )


async def add_document(
    session,
    s3: FakeS3,
    org: uuid.UUID,
    data: bytes,
    content_type: str = "text/markdown",
    *,
    enable: bool = True,
) -> uuid.UUID:
    if enable and await session.get(OrganizationKnowledge, org) is None:
        session.add(OrganizationKnowledge(organization_id=org))
    upload_id = uuid.uuid4()
    key = object_key("knowledge_document", org, upload_id) + ".a1"
    s3.objects[key] = data
    session.add(
        FileUpload(
            id=upload_id,
            organization_id=org,
            purpose="knowledge_document",
            filename="manual.md",
            content_type=content_type,
            s3_key=key,
            size_bytes=len(data),
            uploaded_at=datetime.now(timezone.utc),
        )
    )
    await session.flush()
    doc = KnowledgeDocument(
        organization_id=org,
        file_upload_id=upload_id,
        name="manual.md",
        content_type=content_type,
        size_bytes=len(data),
    )
    session.add(doc)
    await session.commit()
    return doc.id


async def test_markdown_becomes_ready_pieces(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    assert await worker.tick() == 1
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "ready" and doc.error is None
    pieces = (
        (
            await async_session.execute(
                select(KnowledgePiece).where(KnowledgePiece.document_id == doc_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(pieces) == doc.piece_count >= 1
    assert len(pieces[0].embedding) == 384


async def test_csv_rows_and_keyword_search(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    csv = b"Part,Name\nNS-BAT-48V-100,Battery 4.8 kWh\nNS-WB-02,Wall bracket\n"
    await add_document(async_session, s3, org, csv, "text/csv")
    await worker.tick()
    hits = await search_pieces(async_session, org, "price of ns bat 48 v 100", None, 5)
    assert hits[0].piece.csv_row == 1
    assert "Battery" in hits[0].piece.text


async def test_search_never_crosses_workspaces(async_session, worker, s3) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await add_document(async_session, s3, mine, b"# Hours\n\nOpen nine to five.\n")
    await add_document(async_session, s3, theirs, NORTHGATE_MANUAL.encode())
    await worker.tick()
    vector = FakeEmbedder().embed_query("error E-204 grid voltage")
    hits = await search_pieces(async_session, mine, "error E-204", vector, 5)
    assert all(h.piece.text.find("E-204") < 0 for h in hits)
    hits = await search_pieces(async_session, theirs, "error E-204", vector, 5)
    assert hits and "E-204" in hits[0].piece.text


async def test_vector_hits_need_the_floor(
    async_session, worker, s3, monkeypatch
) -> None:
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    vector = FakeEmbedder().embed_query("warranty ten year purchase")
    monkeypatch.setattr(search_module, "MIN_SIMILARITY", -1.0)
    assert await search_pieces(async_session, org, "zzz", vector, 2)
    monkeypatch.setattr(search_module, "MIN_SIMILARITY", 1.01)
    assert not await search_pieces(async_session, org, "zzz", vector, 2)


async def test_piece_limit_refuses_the_file(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    async_session.add(OrganizationKnowledge(organization_id=org, max_pieces=1))
    await async_session.commit()
    rows = "Part\n" + "\n".join(f"P-{i}" for i in range(5))
    doc_id = await add_document(async_session, s3, org, rows.encode(), "text/csv")
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed" and "more than 1 pieces" in doc.error
    count = await async_session.scalar(select(func.count()).select_from(KnowledgePiece))
    assert count == 0


async def test_wrong_type_is_refused(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"not a pdf", "application/pdf")
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed" and "does not look like" in doc.error


async def test_bucket_error_retries_then_fails(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    s3.fail = True
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "pending" and doc.attempts == 1
    # Later attempts: make the row claimable at once.
    for _ in range(MAX_ATTEMPTS):
        doc.claimed_at = None
        await async_session.commit()
        await worker.tick()
        await async_session.refresh(doc)
    assert doc.status == "failed"


async def test_document_off_workspace_is_refused(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello.", enable=False)
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed"


async def test_closed_workspace_knowledge_is_deleted(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    async_session.add(
        OrgClosure(
            organization_id=org, closed_at=datetime.now(timezone.utc), source="test"
        )
    )
    await async_session.commit()
    await worker.tick()
    assert not s3.objects
    for model in (KnowledgeDocument, KnowledgePiece, OrganizationKnowledge):
        assert not await async_session.scalar(select(func.count()).select_from(model))


async def test_delete_org_knowledge_keeps_other_workspaces(
    async_session, worker, s3
) -> None:
    mine, theirs = uuid.uuid4(), uuid.uuid4()
    await add_document(async_session, s3, mine, b"Mine.")
    await add_document(async_session, s3, theirs, b"Theirs.")
    await worker.tick()
    assert await delete_org_knowledge(async_session, mine, s3) == 1
    left = (await async_session.execute(select(KnowledgeDocument))).scalars().all()
    assert [d.organization_id for d in left] == [theirs]


async def test_agent_uses_knowledge_needs_both_switches(async_session) -> None:
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="p", knowledge=True)
    async_session.add(agent)
    await async_session.commit()
    assert not await agent_uses_knowledge(async_session, org, agent.id)
    async_session.add(OrganizationKnowledge(organization_id=org))
    await async_session.commit()
    assert await agent_uses_knowledge(async_session, org, agent.id)
    assert not await agent_uses_knowledge(async_session, uuid.uuid4(), agent.id)


def test_memory_index_hybrid(monkeypatch) -> None:
    fake = FakeEmbedder()
    texts = [
        "Error E-204 means the grid voltage is out of range.",
        "The inverter has a ten year warranty.",
        "Mount it on a vertical wall.",
    ]
    pieces = [Piece(uuid.uuid4(), uuid.uuid4(), "m.md", t) for t in texts]
    index = MemoryIndex(pieces, fake.embed_passages(texts))
    hits = index.search("what does e 204 mean", fake.embed_query("e 204"), 3)
    assert hits[0].piece.text.startswith("Error E-204")
    assert index.search("yes thanks", fake.embed_query("yes thanks"), 3) == []
    monkeypatch.setattr(index_module, "MIN_SIMILARITY", -1.0)
    hits = index.search("zzz", np.asarray(fake.embed_query("warranty")), 1)
    assert hits[0].piece.text.startswith("The inverter")
