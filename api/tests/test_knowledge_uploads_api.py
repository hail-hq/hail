"""Direct knowledge uploads: reserve, sign, complete, cancel, sweep."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID, uuid4

import pytest
from hailhq.api.deps import get_s3_mail_optional
from hailhq.api.main import app
from hailhq.core.email_attachment_gc import EmailAttachmentGcWorker
from hailhq.core.file_uploads import MAX_PENDING_LINKS
from hailhq.core.models import FileUpload, KnowledgeDocument, OrganizationKnowledge
from sqlalchemy import select, text, update

from .conftest import insert_org_and_key

_MD = b"# Warranty\n\nThe NS-5000 inverter has a ten year warranty.\n"
_PDF = "application/pdf"


class FakeS3:
    """An in-memory bucket with the calls the direct upload routes make."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.signed: list[dict[str, Any]] = []

    def put(self, key: str, data: bytes, content_type: str) -> None:
        self.objects[key] = (data, content_type)

    async def head(self, key: str) -> dict[str, Any] | None:
        if key not in self.objects:
            return None
        data, content_type = self.objects[key]
        return {"ContentLength": len(data), "ContentType": content_type}

    async def fetch_head(self, key: str, length: int) -> bytes:
        return self.objects[key][0][:length]

    async def copy(self, source: str, target: str, content_type: str) -> None:
        self.objects[target] = (self.objects[source][0], content_type)

    async def presign(self, operation, key, *, ttl_seconds, http_method, **params):
        self.signed.append(
            {
                "operation": operation,
                "key": key,
                "ttl": ttl_seconds,
                "method": http_method,
                **params,
            }
        )
        return f"https://storage.example.test/{key}?op={operation}"

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    async def delete_prefix(self, prefix: str, *, keep: str | None = None) -> int:
        gone = [k for k in self.objects if k.startswith(prefix) and k != keep]
        for k in gone:
            del self.objects[k]
        return len(gone)


@pytest.fixture()
def s3():
    store = FakeS3()
    app.dependency_overrides[get_s3_mail_optional] = lambda: store
    try:
        yield store
    finally:
        app.dependency_overrides.pop(get_s3_mail_optional, None)


def _auth(plain: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plain}"}


async def _turn_on(session, org, **limits) -> None:
    session.add(OrganizationKnowledge(organization_id=org, **limits))
    await session.commit()


async def _reserve(client, plain, size=len(_MD), content_type="text/markdown"):
    return await client.post(
        "/v1/knowledge/uploads",
        json={
            "filename": "manual.md",
            "content_type": content_type,
            "size_bytes": size,
        },
        headers=_auth(plain),
    )


async def _sign(client, plain, upload_id, **body):
    return await client.post(
        f"/v1/knowledge/uploads/{upload_id}/sign", json=body, headers=_auth(plain)
    )


async def _complete(client, plain, upload_id, **body):
    return await client.post(
        f"/v1/knowledge/uploads/{upload_id}/complete",
        json=body or None,
        headers=_auth(plain),
    )


async def test_reserve_picks_the_key_under_the_org_folder(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    resp = await _reserve(client, plain)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["key"].startswith(f"knowledge-documents/{org}/{body['id']}.")
    assert body["content_type"] == "text/markdown"
    assert body["max_bytes"] == 20 * 1024 * 1024
    row = await async_session.get(FileUpload, UUID(body["id"]))
    assert row.uploaded_at is None and row.token_hash is None
    assert row.link_expires_at is not None and row.size_bytes == len(_MD)


async def test_reserve_checks_access_type_and_limits(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    assert (await _reserve(client, plain)).status_code == 403
    await _turn_on(async_session, org, max_file_bytes=100, max_documents=1)
    assert (await _reserve(client, plain, size=101)).status_code == 413
    assert (await _reserve(client, plain, content_type="image/png")).status_code == 422
    assert (await _reserve(client, plain, size=0)).status_code == 422
    upload_id = (await _reserve(client, plain)).json()["id"]
    s3.put(
        (await async_session.get(FileUpload, UUID(upload_id))).s3_key,
        _MD,
        "text/markdown",
    )
    assert (await _complete(client, plain, upload_id)).status_code == 201
    resp = await _reserve(client, plain)
    assert resp.status_code == 409 and "limit of 1" in resp.json()["detail"]


async def test_reservations_count_toward_the_pending_cap(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    for _ in range(MAX_PENDING_LINKS):
        assert (await _reserve(client, plain)).status_code == 201
    assert (await _reserve(client, plain)).status_code == 429


async def test_sign_maps_the_six_requests_to_the_upload_key(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain, size=12 * 1024 * 1024)).json()
    cases = [
        ({"method": "PUT"}, "put_object", {"content-type": "text/markdown"}),
        (
            {"method": "POST"},
            "create_multipart_upload",
            {"content-type": "text/markdown"},
        ),
        ({"method": "PUT", "s3_upload_id": "U1", "part_number": 3}, "upload_part", {}),
        ({"method": "GET", "s3_upload_id": "U1"}, "list_parts", {}),
        ({"method": "POST", "s3_upload_id": "U1"}, "complete_multipart_upload", {}),
        ({"method": "DELETE", "s3_upload_id": "U1"}, "abort_multipart_upload", {}),
    ]
    for body, operation, headers in cases:
        resp = await _sign(client, plain, up["id"], **body)
        assert resp.status_code == 200, (body, resp.text)
        out = resp.json()
        assert out["key"] == up["key"] and out["headers"] == headers
        signed = s3.signed[-1]
        assert signed["operation"] == operation and signed["key"] == up["key"]
        assert signed["method"] == body["method"]
        assert 0 < signed["ttl"] <= 300
    assert s3.signed[2]["PartNumber"] == 3 and s3.signed[2]["UploadId"] == "U1"
    assert s3.signed[0]["ContentType"] == "text/markdown"


async def test_sign_refuses_other_requests(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain, size=6 * 1024 * 1024)).json()
    for body in (
        {"method": "GET"},
        {"method": "DELETE"},
        {"method": "PATCH"},
        {"method": "PUT", "s3_upload_id": "U1"},
        {"method": "POST", "part_number": 1},
        {"method": "PUT", "part_number": 1},
        {"method": "PUT", "s3_upload_id": "U1", "part_number": 3},
        {"method": "PUT", "s3_upload_id": "a b", "part_number": 1},
        {"method": "PUT", "key": "knowledge-documents/other"},
    ):
        resp = await _sign(client, plain, up["id"], **body)
        assert resp.status_code == 422, body
    assert s3.signed == []


async def test_sign_and_complete_are_org_scoped(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    other_org, _, other = await insert_org_and_key(async_session)
    await _turn_on(async_session, other_org)
    assert (await _sign(client, other, up["id"], method="PUT")).status_code == 404
    assert (await _complete(client, other, up["id"])).status_code == 404
    resp = await client.delete(
        f"/v1/knowledge/uploads/{up['id']}", headers=_auth(other)
    )
    assert resp.status_code == 404
    assert (await _sign(client, plain, uuid4(), method="PUT")).status_code == 404


async def test_sign_refuses_expired_and_turned_off(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    await async_session.execute(
        update(FileUpload)
        .where(FileUpload.id == UUID(up["id"]))
        .values(link_expires_at=text("now() - interval '1 minute'"))
    )
    await async_session.commit()
    assert (await _sign(client, plain, up["id"], method="PUT")).status_code == 410
    up = (await _reserve(client, plain)).json()
    await async_session.delete(await async_session.get(OrganizationKnowledge, org))
    await async_session.commit()
    assert (await _sign(client, plain, up["id"], method="PUT")).status_code == 403


async def test_complete_makes_a_pending_document(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    s3.put(up["key"], _MD, "text/markdown")
    resp = await _complete(client, plain, up["id"], name="Warranty")
    assert resp.status_code == 201, resp.text
    doc = resp.json()
    assert doc["status"] == "pending" and doc["name"] == "Warranty"
    assert doc["size_bytes"] == len(_MD)
    row = await async_session.get(FileUpload, UUID(up["id"]))
    await async_session.refresh(row)
    assert row.uploaded_at is not None and row.first_used_at is not None
    # The checked copy is kept; the signed key is gone.
    assert row.s3_key != up["key"] and list(s3.objects) == [row.s3_key]
    assert s3.objects[row.s3_key][0] == _MD
    stored = (
        await async_session.execute(
            select(KnowledgeDocument).where(KnowledgeDocument.file_upload_id == row.id)
        )
    ).scalar_one()
    assert stored.organization_id == org
    # A second complete finds nothing to complete.
    assert (await _complete(client, plain, up["id"])).status_code == 404
    assert (await _sign(client, plain, up["id"], method="PUT")).status_code == 404


async def test_complete_before_the_file_arrives_keeps_the_upload(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    resp = await _complete(client, plain, up["id"])
    assert resp.status_code == 409
    assert await async_session.get(FileUpload, UUID(up["id"])) is not None
    s3.put(up["key"], _MD, "text/markdown")
    assert (await _complete(client, plain, up["id"])).status_code == 201


@pytest.mark.parametrize(
    ("declared_type", "data", "stored_type", "size_delta", "status"),
    [
        ("text/markdown", _MD, "text/markdown", 1, 422),  # size differs
        (_PDF, b"not a pdf at all", _PDF, 0, 422),  # first bytes differ
        ("text/csv", b"a,b\x00c", "text/csv", 0, 422),  # not text
        ("text/markdown", _MD, "text/html", 0, 422),  # stored type differs
    ],
)
async def test_complete_rejects_a_mismatch_and_deletes_it(
    client,
    async_session,
    org_and_key,
    s3,
    declared_type,
    data,
    stored_type,
    size_delta,
    status,
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (
        await _reserve(
            client, plain, size=len(data) + size_delta, content_type=declared_type
        )
    ).json()
    s3.put(up["key"], data, stored_type)
    resp = await _complete(client, plain, up["id"])
    assert resp.status_code == status, resp.text
    assert s3.objects == {}
    async_session.expunge_all()
    assert await async_session.get(FileUpload, UUID(up["id"])) is None
    docs = (
        (
            await async_session.execute(
                select(KnowledgeDocument).where(
                    KnowledgeDocument.organization_id == org
                )
            )
        )
        .scalars()
        .all()
    )
    assert docs == []


async def test_complete_checks_the_limit_now(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    s3.put(up["key"], _MD, "text/markdown")
    limits = await async_session.get(OrganizationKnowledge, org)
    limits.max_file_bytes = 10
    await async_session.commit()
    resp = await _complete(client, plain, up["id"])
    assert resp.status_code == 413
    assert s3.objects == {}


async def test_complete_a_pdf_bigger_than_the_sniff_window(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    data = b"%PDF-1.7\n" + b"x" * (6 * 1024 * 1024)
    up = (await _reserve(client, plain, size=len(data), content_type=_PDF)).json()
    s3.put(up["key"], data, _PDF)
    resp = await _complete(client, plain, up["id"])
    assert resp.status_code == 201, resp.text


async def test_delete_cancels_and_removes_the_files(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    s3.put(up["key"], _MD, "text/markdown")
    resp = await client.delete(
        f"/v1/knowledge/uploads/{up['id']}", headers=_auth(plain)
    )
    assert resp.status_code == 204
    assert s3.objects == {}
    async_session.expunge_all()
    assert await async_session.get(FileUpload, UUID(up["id"])) is None
    resp = await client.delete(
        f"/v1/knowledge/uploads/{up['id']}", headers=_auth(plain)
    )
    assert resp.status_code == 404


async def test_link_put_cannot_use_a_reservation(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    resp = await client.put(f"/v1/uploads/{up['id']}?token=x", content=_MD)
    assert resp.status_code == 404


async def test_sweep_removes_an_abandoned_reservation(
    client, async_session, org_and_key, s3
) -> None:
    org, _, plain = org_and_key
    await _turn_on(async_session, org)
    up = (await _reserve(client, plain)).json()
    s3.put(up["key"], _MD, "text/markdown")
    await async_session.execute(
        update(FileUpload)
        .where(FileUpload.id == UUID(up["id"]))
        .values(link_expires_at=text("now() - interval '1 hour'"))
    )
    await async_session.commit()

    @asynccontextmanager
    async def session_factory():
        yield async_session

    worker = EmailAttachmentGcWorker(
        session_factory=session_factory, s3_factory=lambda: s3
    )
    assert await worker.tick() == 1
    assert s3.objects == {}
    assert (await _complete(client, plain, up["id"])).status_code == 404
