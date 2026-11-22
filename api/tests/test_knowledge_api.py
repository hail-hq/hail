"""The knowledge base API: access, limits, documents, search, isolation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.api.routes import knowledge as knowledge_routes
from hailhq.api.routes import uploads as uploads_routes
from hailhq.core.config import settings
from hailhq.core.file_uploads import object_key
from hailhq.core.knowledge.search import vector_literal
from hailhq.core.knowledge.text import terms
from hailhq.core.models import (
    FileUpload,
    KnowledgeDocument,
    OrganizationKnowledge,
)
from hailhq.core.testing.knowledge import FakeEmbedder
from sqlalchemy import select, text

from .conftest import insert_org_and_key

_MD = b"# Warranty\n\nThe NS-5000 inverter has a ten year warranty.\n"


@pytest.fixture()
def s3(monkeypatch: pytest.MonkeyPatch):
    store = AsyncMock()
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    monkeypatch.setattr(settings, "hail_api_url", "https://api.example.test/")
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)


@pytest.fixture(autouse=True)
def fake_model(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(knowledge_routes, "get_embedder", FakeEmbedder)


def _auth(plain: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plain}"}


async def _turn_on(session, org, **limits) -> None:
    session.add(OrganizationKnowledge(organization_id=org, **limits))
    await session.commit()


async def _link(client, plain, content_type="text/markdown"):
    return await client.post(
        "/v1/uploads",
        json={
            "purpose": "knowledge_document",
            "filename": "manual.md",
            "content_type": content_type,
        },
        headers=_auth(plain),
    )


async def _upload(client, plain, data=_MD, content_type="text/markdown") -> str:
    link = (await _link(client, plain, content_type)).json()
    parts = urlsplit(link["upload_url"])
    resp = await client.put(f"{parts.path}?{parts.query}", content=data)
    assert resp.status_code == 200, resp.text
    return link["id"]


async def _stored_upload(session, org, data=_MD) -> FileUpload:
    upload_id = uuid4()
    row = FileUpload(
        id=upload_id,
        organization_id=org,
        purpose="knowledge_document",
        filename="manual.md",
        content_type="text/markdown",
        s3_key=object_key("knowledge_document", org, upload_id) + ".a",
        size_bytes=len(data),
        uploaded_at=datetime.now(timezone.utc),
    )
    session.add(row)
    await session.commit()
    return row


async def _piece(session, org, doc_id, body: str, ordinal: int = 0) -> None:
    await session.execute(
        text(
            "INSERT INTO knowledge_pieces (document_id, organization_id, ordinal, "
            "text, embedding, terms) VALUES (:d, :o, :n, :t, CAST(:e AS vector), "
            "array_to_tsvector(CAST(:w AS text[])))"
        ),
        {
            "d": doc_id,
            "o": org,
            "n": ordinal,
            "t": body,
            "e": vector_literal(FakeEmbedder().embed_query(body)),
            "w": list(dict.fromkeys(terms(body))),
        },
    )
    await session.commit()


async def test_off_workspace_gets_403_everywhere(client, org_and_key, s3) -> None:
    _, _, plain = org_and_key
    assert (await _link(client, plain)).status_code == 403
    for method, path, body in (
        ("POST", "/v1/knowledge/documents", {"upload_id": str(uuid4())}),
        ("POST", "/v1/knowledge/search", {"query": "warranty"}),
    ):
        resp = await client.request(method, path, json=body, headers=_auth(plain))
        assert resp.status_code == 403, path
        assert "not turned on" in resp.json()["detail"]
    resp = await client.post(
        "/v1/agents",
        json={"name": "a", "system_prompt": "p", "knowledge": True},
        headers=_auth(plain),
    )
    assert resp.status_code == 403
    # A workspace can always see its own files, to remove them.
    listed = await client.get("/v1/knowledge/documents", headers=_auth(plain))
    assert listed.status_code == 200 and listed.json()["items"] == []
    missing = await client.get(
        f"/v1/knowledge/documents/{uuid4()}", headers=_auth(plain)
    )
    assert missing.status_code == 404


async def test_link_takes_the_workspace_file_limit(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org, max_file_bytes=100)
    link = (await _link(client, plain)).json()
    assert link["max_bytes"] == 100
    parts = urlsplit(link["upload_url"])
    resp = await client.put(f"{parts.path}?{parts.query}", content=b"a" * 101)
    assert resp.status_code == 413


async def test_put_checks_the_limit_again(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    link = (await _link(client, plain)).json()
    limits = await async_session.get(OrganizationKnowledge, org)
    limits.max_file_bytes = 10
    await async_session.commit()
    parts = urlsplit(link["upload_url"])
    resp = await client.put(f"{parts.path}?{parts.query}", content=b"a" * 11)
    assert resp.status_code == 413
    await async_session.delete(limits)
    await async_session.commit()
    resp = await client.put(f"{parts.path}?{parts.query}", content=b"a")
    assert resp.status_code == 403


async def test_put_sniffs_the_declared_type(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    link = (await _link(client, plain, "application/pdf")).json()
    parts = urlsplit(link["upload_url"])
    resp = await client.put(f"{parts.path}?{parts.query}", content=b"not a pdf")
    assert resp.status_code == 422
    link = (await _link(client, plain, "text/csv")).json()
    parts = urlsplit(link["upload_url"])
    resp = await client.put(f"{parts.path}?{parts.query}", content=b"a\x00b")
    assert resp.status_code == 422


async def test_storage_full_refuses_links_and_documents(
    client, async_session, org_and_key, s3, monkeypatch
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    upload = await _stored_upload(async_session, org)
    full = AsyncMock(return_value=True)
    monkeypatch.setattr(uploads_routes, "storage_full", full)
    monkeypatch.setattr(knowledge_routes, "storage_full", full)
    assert (await _link(client, plain)).status_code == 409
    resp = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": str(upload.id)},
        headers=_auth(plain),
    )
    assert resp.status_code == 409 and "storage is full" in resp.json()["detail"]


async def test_add_list_get_delete(client, async_session, org_and_key, s3) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    upload_id = await _upload(client, plain)
    resp = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": upload_id, "name": "Inverter manual"},
        headers=_auth(plain),
    )
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["status"] == "pending" and doc["name"] == "Inverter manual"
    again = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": upload_id},
        headers=_auth(plain),
    )
    assert again.status_code == 409
    listed = (await client.get("/v1/knowledge/documents", headers=_auth(plain))).json()
    assert [d["id"] for d in listed["items"]] == [doc["id"]]
    got = await client.get(f"/v1/knowledge/documents/{doc['id']}", headers=_auth(plain))
    assert got.json()["content_type"] == "text/markdown"
    resp = await client.delete(
        f"/v1/knowledge/documents/{doc['id']}", headers=_auth(plain)
    )
    assert resp.status_code == 204
    s3.delete_prefix.assert_awaited()
    assert await async_session.get(FileUpload, upload_id) is None
    assert not (await async_session.execute(select(KnowledgeDocument))).all()


async def test_document_limit(client, async_session, org_and_key, s3) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org, max_documents=1)
    for expected in (201, 409):
        upload = await _stored_upload(async_session, org)
        resp = await client.post(
            "/v1/knowledge/documents",
            json={"upload_id": str(upload.id)},
            headers=_auth(plain),
        )
        assert resp.status_code == expected


async def test_workspaces_never_see_each_other(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    other, _, other_plain = await insert_org_and_key(
        async_session, org_name="Globex", org_slug="globex"
    )
    await _turn_on(async_session, org)
    await _turn_on(async_session, other)
    upload = await _stored_upload(async_session, other)
    # Another workspace's upload cannot be added.
    resp = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": str(upload.id)},
        headers=_auth(plain),
    )
    assert resp.status_code == 404
    resp = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": str(upload.id)},
        headers=_auth(other_plain),
    )
    doc_id = resp.json()["id"]
    await _piece(async_session, other, doc_id, "Error E-204 means grid voltage.")
    for path in (f"/v1/knowledge/documents/{doc_id}",):
        assert (await client.get(path, headers=_auth(plain))).status_code == 404
        assert (await client.delete(path, headers=_auth(plain))).status_code == 404
    listed = await client.get("/v1/knowledge/documents", headers=_auth(plain))
    assert listed.json()["items"] == []
    mine = await client.post(
        "/v1/knowledge/search", json={"query": "error E-204"}, headers=_auth(plain)
    )
    assert mine.json()["results"] == []
    theirs = await client.post(
        "/v1/knowledge/search",
        json={"query": "error E-204"},
        headers=_auth(other_plain),
    )
    assert theirs.json()["results"][0]["text"].startswith("Error E-204")


async def test_agent_knowledge_switch(client, async_session, org_and_key) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    resp = await client.post(
        "/v1/agents",
        json={"name": "a", "system_prompt": "p", "knowledge": True},
        headers=_auth(plain),
    )
    assert resp.status_code == 201 and resp.json()["knowledge"] is True
    agent_id = resp.json()["id"]
    await async_session.delete(await async_session.get(OrganizationKnowledge, org))
    await async_session.commit()
    resp = await client.patch(
        f"/v1/agents/{agent_id}", json={"knowledge": False}, headers=_auth(plain)
    )
    assert resp.status_code == 200 and resp.json()["knowledge"] is False
    resp = await client.patch(
        f"/v1/agents/{agent_id}", json={"knowledge": True}, headers=_auth(plain)
    )
    assert resp.status_code == 403
