"""The knowledge ingest worker and the SQL hybrid search, on a real
Postgres with pgvector."""

from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from botocore.exceptions import ClientError
from hailhq.core.file_uploads import object_key
from hailhq.core.knowledge import index as index_module
from hailhq.core.knowledge import ingest as ingest_module
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
        self.error_code: str | None = None

    async def fetch_raw(self, key: str, *, max_bytes: int | None = None) -> bytes:
        # Like S3MailClient.fetch_raw: a ValueError past max_bytes.
        if self.fail:
            raise ConnectionError("bucket down")
        if self.error_code:
            raise ClientError({"Error": {"Code": self.error_code}}, "GetObject")
        data = self.objects[key]
        if max_bytes is not None and len(data) > max_bytes:
            raise ValueError("Object exceeds the storage read limit")
        return data

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


async def test_bucket_error_waits_without_using_a_try(
    async_session, worker, s3
) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    s3.fail = True
    for _ in range(MAX_ATTEMPTS + 2):
        doc = await async_session.get(KnowledgeDocument, doc_id)
        doc.claimed_at = None
        await async_session.commit()
        await worker.tick()
        await async_session.refresh(doc)
        assert doc.status == "pending" and doc.attempts == 0
    s3.fail = False
    doc.claimed_at = None
    await async_session.commit()
    await worker.tick()
    await async_session.refresh(doc)
    assert doc.status == "ready"


async def test_other_errors_use_tries_then_fail(
    async_session, session_factory, s3
) -> None:
    class Broken(FakeEmbedder):
        def embed_passages(self, texts):
            raise RuntimeError("model crashed")

    @asynccontextmanager
    async def scope():
        async with session_factory() as session:
            yield session

    worker = KnowledgeIngestWorker(
        session_factory=scope, s3_factory=lambda: s3, embedder=Broken
    )
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    for attempt in range(1, MAX_ATTEMPTS):
        await worker.tick()
        doc = await async_session.get(KnowledgeDocument, doc_id)
        await async_session.refresh(doc)
        assert doc.status == "pending" and doc.attempts == attempt
        doc.claimed_at = None
        await async_session.commit()
    # The last try fails the document at once.
    await worker.tick()
    await async_session.refresh(doc)
    assert doc.status == "failed" and "upload it again" in doc.error


async def test_many_spent_documents_fail_in_one_tick(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    ids = [await add_document(async_session, s3, org, b"Hi.") for _ in range(5)]
    for doc_id in ids:
        doc = await async_session.get(KnowledgeDocument, doc_id)
        doc.attempts = MAX_ATTEMPTS
    await async_session.commit()
    assert await worker.tick() == 0
    for doc_id in ids:
        doc = await async_session.get(KnowledgeDocument, doc_id)
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


async def test_closed_workspace_with_only_uploads_is_cleaned(
    async_session, worker, s3
) -> None:
    """A closed workspace that uploaded files but made no document (and its
    limits row) is found and cleaned too."""
    org = uuid.uuid4()
    async_session.add(OrganizationKnowledge(organization_id=org))
    upload_id = uuid.uuid4()
    key = object_key("knowledge_document", org, upload_id) + ".a"
    s3.objects[key] = b"x"
    async_session.add(
        FileUpload(
            id=upload_id,
            organization_id=org,
            purpose="knowledge_document",
            filename="f.md",
            content_type="text/markdown",
            s3_key=key,
            size_bytes=1,
            uploaded_at=datetime.now(timezone.utc),
        )
    )
    async_session.add(
        OrgClosure(
            organization_id=org, closed_at=datetime.now(timezone.utc), source="test"
        )
    )
    await async_session.commit()
    await worker.tick()
    assert not s3.objects
    assert await async_session.get(FileUpload, upload_id) is None
    assert await async_session.get(OrganizationKnowledge, org) is None


async def test_access_denied_uses_tries_then_fails(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    s3.error_code = "AccessDenied"
    for attempt in range(1, MAX_ATTEMPTS + 1):
        await worker.tick()
        doc = await async_session.get(KnowledgeDocument, doc_id)
        await async_session.refresh(doc)
        assert doc.attempts == attempt
        doc.claimed_at = None
        await async_session.commit()
    assert doc.status == "failed"


async def test_missing_object_fails_at_once(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    s3.error_code = "NoSuchKey"
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed" and "gone" in doc.error


@pytest.mark.parametrize("stored", [b"Hello", b"Hello there!!"])
async def test_stored_size_must_match(async_session, worker, s3, stored) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    key = next(iter(s3.objects))
    s3.objects[key] = stored
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed" and "not the one uploaded" in doc.error


async def test_stale_pending_document_fails(async_session, worker, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    doc = await async_session.get(KnowledgeDocument, doc_id)
    doc.created_at = datetime.now(timezone.utc) - timedelta(days=2)
    await async_session.commit()
    await worker.tick()
    await async_session.refresh(doc)
    assert doc.status == "failed" and "in time" in doc.error


async def test_one_file_has_its_own_piece_cap(
    async_session, worker, s3, monkeypatch
) -> None:
    monkeypatch.setattr(ingest_module, "MAX_DOCUMENT_PIECES", 2)
    org = uuid.uuid4()
    rows = "Part\n" + "\n".join(f"P-{i}" for i in range(5))
    doc_id = await add_document(async_session, s3, org, rows.encode(), "text/csv")
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed" and "one file gives at most 2" in doc.error


async def test_failed_bucket_delete_turns_off_and_keeps_rows(async_session, s3) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    link = FileUpload(
        organization_id=org,
        purpose="knowledge_document",
        filename="next.md",
        content_type="text/markdown",
        s3_key=f"knowledge-documents/{org}/link",
        token_hash="pending-link",
    )
    async_session.add(link)
    await async_session.commit()
    link_id = link.id

    async def broken(prefix, *, keep=None):
        raise ConnectionError("bucket down")

    s3.delete_prefix = broken
    with pytest.raises(ConnectionError):
        await delete_org_knowledge(async_session, org, s3)
    async_session.expire_all()
    # Off at once, and the unused link no longer works.
    assert await async_session.get(OrganizationKnowledge, org) is None
    assert (await async_session.get(FileUpload, link_id)).token_hash is None
    # The rows stay for the retry.
    assert await async_session.get(KnowledgeDocument, doc_id) is not None


async def test_full_delete_empties_the_folder(async_session, s3) -> None:
    org = uuid.uuid4()
    await add_document(async_session, s3, org, b"Old file.")
    # An object whose row is already gone (a cut-off PUT).
    orphan = f"knowledge-documents/{org}/{uuid.uuid4()}.x"
    s3.objects[orphan] = b"left over"
    other = uuid.uuid4()
    await add_document(async_session, s3, other, b"Another workspace.")
    assert await delete_org_knowledge(async_session, org, s3) == 1
    assert not any(k.startswith(f"knowledge-documents/{org}/") for k in s3.objects)
    assert any(k.startswith(f"knowledge-documents/{other}/") for k in s3.objects)


async def test_upload_added_after_turning_on_again_is_kept(async_session, s3) -> None:
    """Turned on again during a delete, a workspace keeps what it adds: the
    folder sweep runs only while it is still off."""
    org = uuid.uuid4()
    old_doc = await add_document(async_session, s3, org, b"Old file.")
    old_keys = set(s3.objects)
    orphan = f"knowledge-documents/{org}/{uuid.uuid4()}.x"
    s3.objects[orphan] = b"left over"
    plain_delete = s3.delete_prefix
    new_id = uuid.uuid4()
    new_key = object_key("knowledge_document", org, new_id) + ".b"

    async def delete_and_turn_on(prefix, *, keep=None):
        result = await plain_delete(prefix, keep=keep)
        if new_key not in s3.objects:
            async_session.add(OrganizationKnowledge(organization_id=org))
            s3.objects[new_key] = b"New file."
            async_session.add(
                FileUpload(
                    id=new_id,
                    organization_id=org,
                    purpose="knowledge_document",
                    filename="new.md",
                    content_type="text/markdown",
                    s3_key=new_key,
                    size_bytes=9,
                    uploaded_at=datetime.now(timezone.utc),
                )
            )
            await async_session.commit()
        return result

    s3.delete_prefix = delete_and_turn_on
    assert await delete_org_knowledge(async_session, org, s3) == 1
    async_session.expire_all()
    assert await async_session.get(KnowledgeDocument, old_doc) is None
    assert not old_keys & set(s3.objects)
    assert await async_session.get(FileUpload, new_id) is not None
    assert new_key in s3.objects
    # Not swept while on: the orphan waits for the next delete.
    assert orphan in s3.objects
