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
from hailhq.core.knowledge.access import delete_org_knowledge
from hailhq.core.knowledge.search import vector_literal
from hailhq.core.knowledge.text import terms
from hailhq.core.models import (
    FileUpload,
    KnowledgeDocument,
    Organization,
    OrganizationKnowledge,
    OrganizationMember,
)
from hailhq.core.testing.knowledge import FakeEmbedder
from sqlalchemy import select, text

from .conftest import insert_org_and_key
from .test_auth_jwt import _configure_env, _install_test_jwks

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
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeEmbedder:
    embedder = FakeEmbedder()
    monkeypatch.setattr(knowledge_routes, "get_embedder", lambda: embedder)
    return embedder


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


async def _add_document(client, plain, upload, *, name=None):
    response = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": str(upload.id), "name": name},
        headers=_auth(plain),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    ("suffix", "declared_type", "signed_type", "disposition"),
    [
        (".attempt", "text/markdown", "text/markdown", "attachment"),
        (f".{'a' * 32}", "application/pdf", "application/pdf", "inline"),
        (".attempt", "text/html", "application/octet-stream", "attachment"),
    ],
)
async def test_file_redirect_signs_the_stored_key_and_original_filename(
    client,
    async_session,
    org_and_key,
    s3,
    suffix,
    declared_type,
    signed_type,
    disposition,
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    upload = await _stored_upload(async_session, org)
    key = object_key("knowledge_document", org, upload.id) + suffix
    upload.s3_key = key
    upload.filename = 'folder\\Hällo "manual"'
    upload.content_type = declared_type
    await async_session.commit()
    doc = await _add_document(client, plain, upload, name="Warranty instructions")
    path = f"/v1/knowledge/documents/{doc['id']}/file"
    s3.presign_get.return_value = "https://storage.example.test/signed"

    assert (await client.get(path)).status_code == 401
    other_org, _, other_plain = await insert_org_and_key(
        async_session, org_name="Other", org_slug="other"
    )
    await _turn_on(async_session, other_org)
    assert (await client.get(path, headers=_auth(other_plain))).status_code == 404
    s3.presign_get.assert_not_awaited()
    response = await client.get(path, headers=_auth(plain), follow_redirects=False)

    assert response.status_code == 302
    assert response.headers["location"] == "https://storage.example.test/signed"
    assert response.headers["cache-control"] == "no-store"
    s3.presign_get.assert_awaited_once_with(
        key,
        ttl_seconds=300,
        response_content_type=signed_type,
        response_content_disposition=(
            f"{disposition}; filename*=UTF-8''H%C3%A4llo%20%22manual%22"
        ),
        response_cache_control="no-store",
    )


async def test_brand_tags_reuse_the_first_spelling_within_a_workspace(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    other_org, _, other_plain = await insert_org_and_key(
        async_session, org_name="Other", org_slug="other"
    )
    await _turn_on(async_session, org)
    await _turn_on(async_session, other_org)
    documents = [
        await _add_document(
            client, token, await _stored_upload(async_session, workspace)
        )
        for workspace, token in [(org, plain), (org, plain), (other_org, other_plain)]
    ]
    assert all(doc["tags"] == [] for doc in documents)

    for doc, token, value, expected in zip(
        documents,
        [plain, plain, other_plain],
        [" Straße  ", "STRASSE", "STRASSE"],
        ["Straße", "Straße", "STRASSE"],
        strict=True,
    ):
        path = f"/v1/knowledge/documents/{doc['id']}"
        tags = [{"type": "brand", "value": expected}]
        response = await client.patch(
            f"{path}/tags", json={"type": "brand", "value": value}, headers=_auth(token)
        )
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == tags
        assert (await client.get(path, headers=_auth(token))).json()["tags"] == tags

    listed = await client.get("/v1/knowledge/documents", headers=_auth(plain))
    assert {doc["id"] for doc in listed.json()["items"]} == {
        documents[0]["id"],
        documents[1]["id"],
    }
    assert all(
        doc["tags"] == [{"type": "brand", "value": "Straße"}]
        for doc in listed.json()["items"]
    )
    for doc in documents[:2]:
        response = await client.delete(
            f"/v1/knowledge/documents/{doc['id']}", headers=_auth(plain)
        )
        assert response.status_code == 204
    remaining = await async_session.execute(
        text("SELECT value FROM knowledge_tag_values")
    )
    assert remaining.scalars().all() == ["STRASSE"]


async def test_brand_move_replaces_the_value_and_clear_keeps_manual_choice(
    client, async_session, org_and_key
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    doc = await _add_document(client, plain, await _stored_upload(async_session, org))
    path = f"/v1/knowledge/documents/{doc['id']}/tags"

    for value, expected in [
        ("Ardent", [{"type": "brand", "value": "Ardent"}]),
        ("Coast", [{"type": "brand", "value": "Coast"}]),
        (None, []),
        (None, []),
    ]:
        response = await client.patch(
            path, json={"type": "brand", "value": value}, headers=_auth(plain)
        )
        assert response.status_code == 200, response.text
        assert response.json()["tags"] == expected

    assignment = await async_session.execute(
        text(
            "SELECT value_key, is_manual FROM knowledge_document_tags "
            "WHERE document_id = :doc AND type = 'brand'"
        ),
        {"doc": doc["id"]},
    )
    assert assignment.one() == (None, True)
    assert (
        await async_session.scalar(text("SELECT count(*) FROM knowledge_tag_values"))
        == 0
    )

    response = await client.patch(
        path, json={"type": "brand", "value": "Ardent"}, headers=_auth(plain)
    )
    assert response.status_code == 200
    assert await delete_org_knowledge(async_session, org, None) == 1
    for table in ("knowledge_document_tags", "knowledge_tag_values"):
        assert await async_session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


async def test_tag_update_rejects_other_workspaces_and_invalid_values(
    client, async_session, org_and_key
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    doc = await _add_document(client, plain, await _stored_upload(async_session, org))
    path = f"/v1/knowledge/documents/{doc['id']}/tags"
    _, _, other_plain = await insert_org_and_key(
        async_session, org_name="Other", org_slug="other"
    )
    body = {"type": "brand", "value": "Ardent"}
    assert (await client.patch(path, json=body)).status_code == 401
    assert (
        await client.patch(path, json=body, headers=_auth(other_plain))
    ).status_code == 404
    for invalid in (
        {"type": "unsupported", "value": "Ardent"},
        {"type": "brand"},
        {"type": "brand", "value": "   "},
        {"type": "brand", "value": "invalid\x00tag"},
    ):
        assert (
            await client.patch(path, json=invalid, headers=_auth(plain))
        ).status_code == 422


@pytest.mark.parametrize(
    ("role", "claims", "expected"),
    [
        ("member", {}, 403),
        ("owner", {}, 200),
        ("admin", {}, 200),
        (None, {"superadmin": True}, 200),
        ("member", {"superadmin": True, "azp": "oauth-client"}, 403),
    ],
    ids=["member", "owner", "admin", "first-party-superadmin", "oauth-member"],
)
async def test_tag_update_requires_a_workspace_admin_for_jwt_users(
    client,
    async_session,
    org_and_key,
    monkeypatch,
    jwks_client_factory,
    base_claims,
    sign_jwt,
    role,
    claims,
    expected,
) -> None:
    _configure_env(monkeypatch)
    _install_test_jwks(monkeypatch, jwks_client_factory)
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    doc = await _add_document(client, plain, await _stored_upload(async_session, org))
    user = uuid4()
    async_session.add(Organization(id=org))
    if role:
        async_session.add(
            OrganizationMember(
                id=uuid4(),
                organization_id=org,
                user_id=user,
                role=role,
                created_at=datetime.now(timezone.utc),
            )
        )
    await async_session.commit()

    token = sign_jwt(
        dict(base_claims(sub=str(user)), activeOrganizationId=str(org), **claims)
    )
    path = f"/v1/knowledge/documents/{doc['id']}"
    response = await client.patch(
        f"{path}/tags",
        json={"type": "brand", "value": "Ardent"},
        headers=_auth(token),
    )
    assert response.status_code == expected, response.text
    read = await client.get(path, headers=_auth(token))
    assert read.status_code == 200
    assert read.json()["tags"] == (
        [{"type": "brand", "value": "Ardent"}] if expected == 200 else []
    )


async def _piece(session, org, doc_id, body: str, ordinal: int = 0) -> None:
    await session.execute(
        text(
            "INSERT INTO knowledge_pieces (document_id, organization_id, ordinal, "
            "text, embedding, embedding_model, terms) VALUES (:d, :o, :n, :t, "
            "CAST(:e AS halfvec), 'fake-embedding', "
            "array_to_tsvector(CAST(:w AS text[])))"
        ),
        {
            "d": doc_id,
            "o": org,
            "n": ordinal,
            "t": body,
            "e": vector_literal(FakeEmbedder().one(body)),
            "w": list(dict.fromkeys(terms(body))),
        },
    )
    await session.commit()


async def test_off_workspace_gets_403_on_add_and_search(
    client, org_and_key, s3
) -> None:
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


async def test_search_falls_back_to_keywords_on_embedding_error(
    client, async_session, org_and_key, fake
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    upload = await _stored_upload(async_session, org)
    resp = await client.post(
        "/v1/knowledge/documents",
        json={"upload_id": str(upload.id)},
        headers=_auth(plain),
    )
    await _piece(async_session, org, resp.json()["id"], "Error E-204 means grid.")
    fake.fail = True
    found = await client.post(
        "/v1/knowledge/search", json={"query": "error E-204"}, headers=_auth(plain)
    )
    assert found.status_code == 200
    assert found.json()["results"][0]["text"].startswith("Error E-204")
    assert fake.calls == 1


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


async def test_busy_upload_answers_503(
    client, async_session, session_factory, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    upload = await _stored_upload(async_session, org)
    async with session_factory() as holder:
        await holder.execute(
            select(FileUpload).where(FileUpload.id == upload.id).with_for_update()
        )
        resp = await client.post(
            "/v1/knowledge/documents",
            json={"upload_id": str(upload.id)},
            headers=_auth(plain),
        )
        await holder.rollback()
    assert resp.status_code == 503 and resp.headers["Retry-After"] == "5"
