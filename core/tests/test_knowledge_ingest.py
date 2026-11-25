"""The knowledge ingest worker and the SQL hybrid search, on a real
Postgres with pgvector."""

from __future__ import annotations

import builtins
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest
from botocore.exceptions import ClientError
from hailhq.core.config import settings
from hailhq.core.file_uploads import object_key
from hailhq.core.knowledge import access as access_module
from hailhq.core.knowledge import ingest as ingest_module
from hailhq.core.knowledge.access import (
    STORAGE_FULL,
    FolderSweepFailed,
    agent_uses_knowledge,
    delete_org_knowledge,
    involves_database,
    knowledge_bytes,
    log_failure,
    short_cause,
)
from hailhq.core.knowledge.embed import EmbeddingUnavailable
from hailhq.core.knowledge.index import MemoryIndex, Piece
from hailhq.core.knowledge.ingest import MAX_ATTEMPTS, KnowledgeIngestWorker
from hailhq.core.knowledge.search import search_pieces, vector_literal
from hailhq.core.knowledge.text import terms
from hailhq.core.models import (
    Agent,
    FileUpload,
    KnowledgeDocument,
    KnowledgePiece,
    OrganizationKnowledge,
    OrgClosure,
)
from hailhq.core.testing.knowledge import NORTHGATE_MANUAL, FakeEmbedder
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError, StatementError


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
def fake() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture()
def worker(session_factory, s3, fake) -> KnowledgeIngestWorker:
    @asynccontextmanager
    async def scope():
        async with session_factory() as session:
            yield session

    return KnowledgeIngestWorker(
        session_factory=scope, s3_factory=lambda: s3, embedder=lambda: fake
    )


async def pieces_of(session, org: uuid.UUID) -> list[KnowledgePiece]:
    session.expire_all()
    return list(
        (
            await session.execute(
                select(KnowledgePiece)
                .where(KnowledgePiece.organization_id == org)
                .order_by(KnowledgePiece.ordinal)
            )
        ).scalars()
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
    # Half precision, unit length, with the model that made it.
    assert len(pieces[0].embedding) == 1536
    assert pieces[0].embedding_model == "fake-embedding"
    assert abs(np.linalg.norm(pieces[0].embedding) - 1) < 1e-2


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
    vector = FakeEmbedder().one("error E-204 grid voltage")
    model = "fake-embedding"
    hits = await search_pieces(
        async_session, mine, "error E-204", vector, 5, model=model
    )
    assert all(h.piece.text.find("E-204") < 0 for h in hits)
    hits = await search_pieces(
        async_session, theirs, "error E-204", vector, 5, model=model
    )
    assert hits and "E-204" in hits[0].piece.text


async def test_vector_hits_need_the_floor(
    async_session, worker, s3, monkeypatch
) -> None:
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    vector = FakeEmbedder().one("warranty ten year purchase")
    model = "fake-embedding"
    monkeypatch.setattr(settings, "hail_embedding_min_similarity", -1.0)
    assert await search_pieces(async_session, org, "zzz", vector, 2, model=model)
    # Another model's vectors are never compared.
    assert not await search_pieces(async_session, org, "zzz", vector, 2, model="m2")
    monkeypatch.setattr(settings, "hail_embedding_min_similarity", 1.01)
    assert not await search_pieces(async_session, org, "zzz", vector, 2, model=model)


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
    async_session, worker, s3, monkeypatch
) -> None:
    def broken(decoded, cap):
        raise RuntimeError("chunking crashed")

    monkeypatch.setattr(ingest_module, "_chunks", broken)
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
    index = MemoryIndex(pieces, np.stack([fake.one(t) for t in texts]))
    hits = index.search("what does e 204 mean", fake.one("e 204"), 3)
    assert hits[0].piece.text.startswith("Error E-204")
    assert index.search("yes thanks", fake.one("yes thanks"), 3) == []
    monkeypatch.setattr(settings, "hail_embedding_min_similarity", -1.0)
    hits = index.search("zzz", fake.one("warranty"), 1)
    assert hits[0].piece.text.startswith("The inverter")


def test_memory_index_piece_without_vector_is_found_by_words(monkeypatch) -> None:
    """A zero row (no vector of the current model) never matches by meaning,
    even with no floor, but its words still find it."""
    fake = FakeEmbedder()
    texts = ["The inverter has a ten year warranty.", "Mount it on a wall."]
    pieces = [Piece(uuid.uuid4(), uuid.uuid4(), "m.md", t) for t in texts]
    vectors = np.stack([np.zeros(1536, np.float32), fake.one(texts[1])])
    index = MemoryIndex(pieces, vectors)
    monkeypatch.setattr(settings, "hail_embedding_min_similarity", -1.0)
    hits = index.search("zzz", fake.one("warranty"), 2)
    assert [h.piece.text for h in hits] == [texts[1]]
    hits = index.search("ten year warranty", None, 2)
    assert hits[0].piece.text == texts[0]


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


async def test_failed_folder_sweep_raises_after_rows_and_reruns(
    async_session, s3
) -> None:
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, b"Hello there.")
    orphan = f"knowledge-documents/{org}/{uuid.uuid4()}.x"
    s3.objects[orphan] = b"left over"
    plain_delete = s3.delete_prefix

    async def folder_fails(prefix, *, keep=None):
        if prefix == f"knowledge-documents/{org}/":
            raise ConnectionError("bucket down")
        return await plain_delete(prefix, keep=keep)

    s3.delete_prefix = folder_fails
    with pytest.raises(FolderSweepFailed) as failed:
        await delete_org_knowledge(async_session, org, s3)
    assert failed.value.documents == 1
    async_session.expire_all()
    assert await async_session.get(KnowledgeDocument, doc_id) is None
    assert orphan in s3.objects
    # Run again: the sweep finishes.
    s3.delete_prefix = plain_delete
    assert await delete_org_knowledge(async_session, org, s3) == 0
    assert orphan not in s3.objects


async def test_listed_upload_cannot_become_a_document_mid_delete(
    async_session, s3
) -> None:
    org = uuid.uuid4()
    async_session.add(OrganizationKnowledge(organization_id=org))
    upload = FileUpload(
        organization_id=org,
        purpose="knowledge_document",
        filename="f.md",
        content_type="text/markdown",
        s3_key=f"knowledge-documents/{org}/u",
        size_bytes=1,
        uploaded_at=datetime.now(timezone.utc),
    )
    async_session.add(upload)
    await async_session.commit()
    first = []
    plain_delete = s3.delete_prefix

    async def check(prefix, *, keep=None):
        if not first:
            row = await async_session.get(FileUpload, upload.id)
            await async_session.refresh(row)
            first.append(row.first_used_at is not None)
        return await plain_delete(prefix, keep=keep)

    s3.delete_prefix = check
    await delete_org_knowledge(async_session, org, s3)
    # Marked used before the first storage call.
    assert first == [True]


def test_sweep_failure_message_is_short() -> None:
    long = FolderSweepFailed(2, RuntimeError("AccessDenied\n" + "x" * 500))
    assert "(RuntimeError: AccessDenied)" in str(long)
    one_line = FolderSweepFailed(1, RuntimeError("y" * 500))
    assert str(one_line).count("y") == 200
    empty = FolderSweepFailed(1, TimeoutError())
    assert "(TimeoutError)" in str(empty)


def test_database_errors_never_show_their_text() -> None:
    db_error = OperationalError("DELETE FROM x WHERE id = :id", {"id": "v"}, None)
    assert "DELETE" not in short_cause(db_error) and "v" not in short_cause(db_error)
    assert involves_database(StatementError("bad", "SELECT 1", {}, None))
    try:
        try:
            raise db_error
        except OperationalError:
            raise RuntimeError("later") from None
    except RuntimeError as wrapped:
        # ``from None`` hides the cause but the context still holds it.
        assert involves_database(wrapped)
    assert not involves_database(RuntimeError("plain"))
    group = getattr(builtins, "ExceptionGroup")  # noqa: B009 (3.11+ name)
    assert involves_database(group("tasks", [db_error]))


def test_log_failure_shows_shape_never_messages(caplog) -> None:
    log = logging.getLogger("test.knowledge")
    try:
        try:
            raise ValueError("text from the customer's file")
        except ValueError as err:
            raise OSError("connection refused") from err
    except OSError as exc:
        with caplog.at_level(logging.ERROR, logger="test.knowledge"):
            log_failure(log, "step failed", exc)
    text = caplog.text
    assert "OSError" in text and "ValueError" in text
    assert "customer" not in text and "refused" not in text


def test_log_failure_shows_group_members_and_their_causes(caplog) -> None:
    log = logging.getLogger("test.knowledge")
    try:
        raise KeyError("secret key")
    except KeyError as err:
        member = RuntimeError("wrapped")
        member.__cause__ = err
    group = getattr(builtins, "ExceptionGroup")("tasks", [member])  # noqa: B009
    with caplog.at_level(logging.ERROR, logger="test.knowledge"):
        log_failure(log, "step failed", group)
    assert "[RuntimeError" in caplog.text and "KeyError" in caplog.text
    assert "secret" not in caplog.text


async def test_embedding_api_failure_keeps_keywords_then_retries(
    async_session, worker, s3, fake
) -> None:
    """An embedding API error never fails the document: its pieces are
    ready and found by words, and a later pass (after the backoff) embeds
    them."""
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    fake.fail = True
    assert await worker.tick() == 1
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "ready"
    pieces = await pieces_of(async_session, org)
    assert pieces and all(p.embedding is None for p in pieces)
    assert all(p.embedding_model is None for p in pieces)
    hits = await search_pieces(async_session, org, "error E-204", None, 3)
    assert hits and "E-204" in hits[0].piece.text
    # Waiting: the next tick sends nothing.
    fake.fail = False
    calls = fake.calls
    await worker.tick()
    assert fake.calls == calls
    worker._embed_after = 0.0
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert all(p.embedding_model == "fake-embedding" for p in pieces)


async def test_model_change_reembeds_every_piece(
    async_session, worker, s3, fake, monkeypatch
) -> None:
    """After the configured model changes, search compares only vectors of
    the new model, and the embedding pass replaces the old ones."""
    monkeypatch.setattr(settings, "hail_embedding_min_similarity", -1.0)
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    vector = fake.one("warranty")
    assert await search_pieces(
        async_session, org, "zzz", vector, 3, model="fake-embedding"
    )
    fake.model = "fake-embedding-2"
    # Before the pass: no vector of the new model, keywords still work.
    assert not await search_pieces(
        async_session, org, "zzz", vector, 3, model="fake-embedding-2"
    )
    assert await search_pieces(
        async_session, org, "warranty", vector, 3, model="fake-embedding-2"
    )
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert pieces and all(p.embedding_model == "fake-embedding-2" for p in pieces)
    assert await search_pieces(
        async_session, org, "zzz", vector, 3, model="fake-embedding-2"
    )


async def test_embedding_pass_skips_workspaces_turned_off(
    async_session, worker, s3, fake
) -> None:
    org = uuid.uuid4()
    fake.fail = True
    await add_document(async_session, s3, org, b"# Hours\n\nOpen nine to five.\n")
    await worker.tick()
    await async_session.delete(await async_session.get(OrganizationKnowledge, org))
    await async_session.commit()
    fake.fail = False
    worker._embed_after = 0.0
    calls = fake.calls
    await worker.tick()
    assert fake.calls == calls
    assert all(p.embedding is None for p in await pieces_of(async_session, org))


async def test_no_embedding_api_means_keyword_search(
    async_session, session_factory, s3
) -> None:
    @asynccontextmanager
    async def scope():
        async with session_factory() as session:
            yield session

    worker = KnowledgeIngestWorker(
        session_factory=scope, s3_factory=lambda: s3, embedder=lambda: None
    )
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    assert await worker.tick() == 1
    assert all(p.embedding is None for p in await pieces_of(async_session, org))
    hits = await search_pieces(async_session, org, "warranty purchase", None, 3)
    assert hits and "warranty" in hits[0].piece.text


async def test_vector_index_keeps_a_small_workspace_results(
    async_session, monkeypatch
) -> None:
    """The HNSW index filters after its scan; with many nearer pieces of
    another workspace, the small workspace still gets its own piece."""
    monkeypatch.setattr(settings, "hail_embedding_min_similarity", -1.0)
    fake = FakeEmbedder()
    big, small = uuid.uuid4(), uuid.uuid4()
    query = fake.one("warranty ten year purchase")
    rows = []
    for org, count, body in (
        (big, 300, "warranty ten year purchase"),
        # Shares a word with the question, so it is reachable in the graph,
        # but every piece of the other workspace is nearer.
        (small, 1, "warranty desk opening hours nine to five"),
    ):
        upload = FileUpload(
            id=uuid.uuid4(),
            organization_id=org,
            purpose="knowledge_document",
            filename="f.md",
            content_type="text/markdown",
            s3_key=f"k/{org}",
            size_bytes=1,
        )
        doc = KnowledgeDocument(
            organization_id=org,
            file_upload_id=upload.id,
            name="f.md",
            content_type="text/markdown",
            size_bytes=1,
            status="ready",
        )
        async_session.add(upload)
        await async_session.flush()
        async_session.add(doc)
        await async_session.flush()
        for i in range(count):
            vector = fake.one(f"{body} {i}" if org == big else body)
            rows.append(
                KnowledgePiece(
                    document_id=doc.id,
                    organization_id=org,
                    ordinal=i,
                    text=body,
                    embedding=vector_literal(vector),
                    embedding_model="fake-embedding",
                    terms="x",
                )
            )
    async_session.add_all(rows)
    await async_session.commit()
    # Make the planner use the HNSW index: no other way to the rows in
    # this transaction (rolled back below).
    await async_session.execute(text("DROP INDEX knowledge_pieces_org_idx"))
    await async_session.execute(text("SET LOCAL enable_seqscan = off"))
    await async_session.execute(text("SET LOCAL hnsw.ef_search = 10"))
    plan = await async_session.execute(
        text(
            "EXPLAIN SELECT id FROM knowledge_pieces WHERE organization_id = :o "
            "AND embedding_model = 'fake-embedding' ORDER BY embedding <=> "
            "CAST(:v AS halfvec(1536)) LIMIT 20"
        ),
        {"o": small, "v": vector_literal(query)},
    )
    assert "knowledge_pieces_embedding_idx" in "\n".join(r[0] for r in plan)
    hits = await search_pieces(
        async_session, small, "zzz", query, 3, model="fake-embedding"
    )
    assert [h.piece.text for h in hits] == ["warranty desk opening hours nine to five"]
    await async_session.rollback()


async def test_refused_piece_never_holds_up_the_others(
    async_session, worker, s3, fake
) -> None:
    """A batch the API refuses is sent again one piece at a time: the
    refused piece is skipped (keywords only), every other piece gets its
    vector, and no backoff starts."""
    org = uuid.uuid4()
    rows = "Part,Name\n" + "\n".join(f"P-{i},Item {i}" for i in range(5))
    await add_document(
        async_session, s3, org, (rows + "\nP-9,poison\n").encode(), "text/csv"
    )
    fake.refuse = "poison"
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert [p.embedding_model is None for p in pieces] == [False] * 5 + [True]
    assert worker._embed_after == 0.0
    calls = fake.calls
    await worker.tick()
    assert fake.calls == calls


def test_backoff_never_overflows(worker) -> None:
    worker._embed_failures = 5000
    worker._back_off(EmbeddingUnavailable("HTTP 401"), 1)
    assert worker._embed_after > 0


async def test_refused_everything_is_a_setting_error(
    async_session, worker, s3, fake
) -> None:
    """When the probe text is refused too (a bad key or width also answers
    400), nothing is skipped: the pass backs off and tries every piece
    later."""
    org = uuid.uuid4()
    rows = "Part,Name\n" + "\n".join(f"P-{i},Item {i}" for i in range(6))
    await add_document(async_session, s3, org, rows.encode(), "text/csv")
    fake.refuse = ""  # every text holds the empty string: all refused
    await worker.tick()
    assert worker._embed_after > 0
    pieces = await pieces_of(async_session, org)
    assert all(p.embedding_refused is None for p in pieces)
    # One batch request and the probe, not one per piece.
    assert fake.calls == 2
    fake.refuse = None
    worker._embed_after = 0.0
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert all(p.embedding_model == "fake-embedding" for p in pieces)


async def test_refused_first_pieces_never_stop_the_rest(
    async_session, worker, s3, fake
) -> None:
    """Refused pieces at the front of the queue are skipped; the rest get
    their vectors in the same pass."""
    org = uuid.uuid4()
    rows = "Part,Name\n" + "\n".join(
        f"P-{i},{'poison' if i < 4 else 'Item'} {i}" for i in range(8)
    )
    await add_document(async_session, s3, org, rows.encode(), "text/csv")
    fake.refuse = "poison"
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert [p.embedding_model is None for p in pieces] == [True] * 4 + [False] * 4
    assert worker._embed_after == 0.0
    marks = [p.embedding_refused for p in pieces]
    assert marks == ["fake-embedding"] * 4 + [None] * 4


async def test_one_workspace_refused_pieces_never_stop_another(
    async_session, worker, s3, fake
) -> None:
    """One workspace's pieces that the API refuses alone are marked and
    leave the queue; another workspace's pieces get their vectors in the
    same pass, and the refused ones are not sent again."""
    bad, good = uuid.uuid4(), uuid.uuid4()
    rows = "Part,Name\n" + "\n".join(f"P-{i},poison {i}" for i in range(120))
    await add_document(async_session, s3, bad, rows.encode(), "text/csv")
    await add_document(async_session, s3, good, NORTHGATE_MANUAL.encode())
    fake.refuse = "poison"
    await worker.tick()
    good_pieces = await pieces_of(async_session, good)
    assert good_pieces and all(p.embedding_model for p in good_pieces)
    bad_pieces = await pieces_of(async_session, bad)
    assert all(p.embedding_refused == "fake-embedding" for p in bad_pieces)
    assert worker._embed_after == 0.0
    calls = fake.calls
    await worker.tick()
    assert fake.calls == calls
    # Keywords still find the refused pieces.
    hits = await search_pieces(async_session, bad, "poison 7", None, 3)
    assert hits
    # A day later they are tried again; so does a new model.
    await async_session.execute(
        text(
            "UPDATE knowledge_pieces SET embedding_refused_at = now() - "
            "interval '2 days' WHERE organization_id = :o"
        ),
        {"o": bad},
    )
    await async_session.commit()
    calls = fake.calls
    before = {p.id: p.embedding_refused_at for p in await pieces_of(async_session, bad)}
    await worker.tick()
    assert fake.calls > calls
    again = await pieces_of(async_session, bad)
    assert all(p.embedding_refused_at > before[p.id] for p in again)
    # Refused again today: no request on the next tick.
    calls = fake.calls
    await worker.tick()
    assert fake.calls == calls
    fake.model, fake.refuse = "fake-embedding-2", None
    await worker.tick()
    assert all(p.embedding_model for p in await pieces_of(async_session, bad))


async def test_never_refused_pieces_go_before_refused_ones(
    async_session, worker, s3, fake, monkeypatch
) -> None:
    """In a workspace's turn, a piece this model refused a day ago comes
    after pieces never refused."""
    org = uuid.uuid4()
    rows = "Part,Name\nP-1,first\nP-2,second\n"
    await add_document(async_session, s3, org, rows.encode(), "text/csv")
    fake.fail = True
    await worker.tick()
    fake.fail = False
    worker._embed_after = 0.0
    first, second = await pieces_of(async_session, org)
    await async_session.execute(
        text(
            "UPDATE knowledge_pieces SET embedding_refused = 'fake-embedding', "
            "embedding_refused_at = now() - interval '2 days' WHERE id = :i"
        ),
        {"i": first.id},
    )
    await async_session.commit()
    monkeypatch.setattr(ingest_module, "BATCH", 1)
    monkeypatch.setattr(ingest_module, "EMBED_PER_TICK", 1)
    await worker.tick()
    first, second = await pieces_of(async_session, org)
    assert second.embedding is not None and first.embedding is None


async def test_live_data_counts_text_and_every_vector(
    async_session, session_factory, s3, fake
) -> None:
    """The cap counts each live piece's text plus one vector, whether the
    vector is written yet or not, and a model change never moves it: old
    vectors (dead rows) do not count."""
    embedder: dict = {"api": None}

    @asynccontextmanager
    async def scope():
        async with session_factory() as session:
            yield session

    worker = KnowledgeIngestWorker(
        session_factory=scope,
        s3_factory=lambda: s3,
        embedder=lambda: embedder["api"],
    )
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    expected = sum(
        access_module.piece_bytes(p.text, list(dict.fromkeys(terms(p.text))))
        for p in pieces
    )
    assert expected > sum(len(p.text.encode()) for p in pieces) + len(pieces) * (
        access_module.PIECE_VECTOR_BYTES
    )
    assert all(p.embedding is None for p in pieces)
    assert await knowledge_bytes(async_session) == expected
    embedder["api"] = fake
    await worker.tick()
    assert await knowledge_bytes(async_session) == expected
    fake.model = "fake-embedding-2"
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert all(p.embedding_model == "fake-embedding-2" for p in pieces)
    assert await knowledge_bytes(async_session) == expected


async def test_model_change_past_the_cap_completes(
    async_session, worker, s3, fake, monkeypatch
) -> None:
    """Replacing vectors never needs room: with the cap already passed, a
    model change still re-embeds every piece."""
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    monkeypatch.setattr(access_module, "GLOBAL_MAX_BYTES", 1)
    fake.model = "fake-embedding-2"
    await worker.tick()
    pieces = await pieces_of(async_session, org)
    assert pieces and all(p.embedding_model == "fake-embedding-2" for p in pieces)


async def test_new_pieces_past_the_live_cap_are_refused(
    async_session, worker, s3, fake, monkeypatch
) -> None:
    """A new file whose text and vectors would pass the live-data cap is
    refused, even with no embedding API set up."""
    org = uuid.uuid4()
    await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    used = await knowledge_bytes(async_session)
    # Room for a little text, not for another piece's vector.
    monkeypatch.setattr(
        access_module, "GLOBAL_MAX_BYTES", used + access_module.PIECE_VECTOR_BYTES
    )
    doc_id = await add_document(async_session, s3, org, b"Opening hours nine to five.")
    await worker.tick()
    doc = await async_session.get(KnowledgeDocument, doc_id)
    await async_session.refresh(doc)
    assert doc.status == "failed" and doc.error == STORAGE_FULL
    # Reading the same document again leaves its own pieces out of the count.
    assert (
        await knowledge_bytes(
            async_session,
            excluding_document=(await pieces_of(async_session, org))[0].document_id,
        )
        == 0
    )


async def test_reading_a_document_again_at_the_cap_is_not_refused(
    async_session, worker, s3, monkeypatch
) -> None:
    """A document read again replaces its own pieces, so at the cap its
    earlier pieces are not counted twice."""
    org = uuid.uuid4()
    doc_id = await add_document(async_session, s3, org, NORTHGATE_MANUAL.encode())
    await worker.tick()
    monkeypatch.setattr(
        access_module, "GLOBAL_MAX_BYTES", await knowledge_bytes(async_session) + 1
    )
    doc = await async_session.get(KnowledgeDocument, doc_id)
    doc.status, doc.attempts, doc.claimed_at = "pending", 0, None
    await async_session.commit()
    assert await worker.tick() == 1
    await async_session.refresh(doc)
    assert doc.status == "ready"
