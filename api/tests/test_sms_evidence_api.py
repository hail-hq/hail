"""POST /verifications/sms/evidence and the public GET of its link."""

from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest
from hailhq.api.main import app
from hailhq.api.routes.sms_evidence import MAX_EVIDENCE_BYTES, get_evidence_store

from .conftest import insert_org_and_key  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
PDF = b"%PDF-1.4 fake"
ORG = "8f6f3c1e-2d4b-4a8e-9b1a-0c2d3e4f5a6b"


def _msg(resp) -> str:
    return "; ".join(d["msg"] for d in resp.json()["detail"])


@pytest.fixture()
def store(monkeypatch):
    monkeypatch.setattr(
        "hailhq.api.routes.sms_evidence.settings.hail_api_url", "https://api.hail.so"
    )
    s3 = AsyncMock()
    s3.count_prefix.return_value = 0
    app.dependency_overrides[get_evidence_store] = lambda: s3
    try:
        yield s3
    finally:
        app.dependency_overrides.pop(get_evidence_store, None)


def _auth(org_and_key) -> dict:
    return {"Authorization": f"Bearer {org_and_key[2]}"}


@pytest.mark.parametrize(
    "data,kind", [(PNG, "image/png"), (JPG, "image/jpeg"), (PDF, "application/pdf")]
)
async def test_upload_returns_a_public_link(
    client: httpx.AsyncClient, org_and_key, store, data, kind
) -> None:
    # The type comes from the bytes, not the header the client sent.
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"file": ("shot", data, "application/octet-stream")},
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["content_type"] == kind
    assert body["size_bytes"] == len(data)
    prefix = f"/v1/verifications/sms/evidence/{org_and_key[0]}/"
    assert prefix in body["url"]
    token = body["url"].split(prefix)[1]
    assert len(token) == 32
    key, payload, content_type = store.put_attachment.await_args.args
    # Stored under the organization, so closing it can delete the folder.
    assert key == f"sms-evidence/{org_and_key[0]}/{token}"
    assert (payload, content_type) == (data, kind)


@pytest.mark.parametrize(
    "data",
    [
        b"<html><script>alert(1)</script></html>",
        b'<svg xmlns="http://www.w3.org/2000/svg"></svg>',
        b"GIF89a",
        b"",
    ],
)
async def test_upload_refuses_other_files(
    client: httpx.AsyncClient, org_and_key, store, data
) -> None:
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"file": ("shot.png", data, "image/png")},
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 422
    assert _msg(resp) == "Upload a PNG, JPG or PDF."
    store.put_attachment.assert_not_awaited()


async def test_upload_refuses_a_big_file(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"file": ("big.png", PNG + b"\x00" * MAX_EVIDENCE_BYTES, "image/png")},
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 422
    assert "10 MB" in _msg(resp)
    store.put_attachment.assert_not_awaited()


async def test_upload_refuses_a_big_body_before_reading_it(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        content=b"x",
        headers={
            **_auth(org_and_key),
            "content-type": "multipart/form-data; boundary=x",
            "content-length": str(20 * 1024 * 1024),
        },
    )
    assert resp.status_code == 422
    assert "10 MB" in _msg(resp)


async def test_upload_needs_one_file_part(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"other": ("shot.png", PNG, "image/png")},
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 422
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files=[
            ("file", ("a.png", PNG, "image/png")),
            ("file", ("b.png", PNG, "image/png")),
        ],
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 422
    store.put_attachment.assert_not_awaited()


async def test_upload_needs_a_key(client: httpx.AsyncClient, store) -> None:
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"file": ("shot.png", PNG, "image/png")},
    )
    assert resp.status_code == 401
    store.put_attachment.assert_not_awaited()


async def test_upload_without_storage_says_paste_a_link(
    client: httpx.AsyncClient, org_and_key
) -> None:
    app.dependency_overrides[get_evidence_store] = lambda: None
    try:
        resp = await client.post(
            "/v1/verifications/sms/evidence",
            files={"file": ("shot.png", PNG, "image/png")},
            headers=_auth(org_and_key),
        )
    finally:
        app.dependency_overrides.pop(get_evidence_store, None)
    assert resp.status_code == 503
    assert "public link" in resp.json()["detail"]


async def test_link_redirects_to_a_short_lived_link(
    client: httpx.AsyncClient, store
) -> None:
    store.presign_get.return_value = "https://storage.example/signed"
    token = "a" * 32
    resp = await client.get(f"/v1/verifications/sms/evidence/{ORG}/{token}")
    assert resp.status_code == 302
    assert resp.headers["location"] == "https://storage.example/signed"
    assert resp.headers["cache-control"] == "no-store"
    # No sign-in, no storage read, no file bytes through the API.
    store.presign_get.assert_awaited_once_with(
        f"sms-evidence/{ORG}/{token}", ttl_seconds=600
    )
    store.fetch_raw.assert_not_awaited()


@pytest.mark.parametrize(
    "path",
    [
        f"{ORG}/short",
        f"{ORG}/{'a' * 33}",
        f"{ORG}/{'a' * 31}.",
        f"not-a-uuid/{'a' * 32}",
    ],
)
async def test_bad_links_are_refused_without_signing(
    client: httpx.AsyncClient, store, path
) -> None:
    resp = await client.get(f"/v1/verifications/sms/evidence/{path}")
    assert resp.status_code in (404, 422)
    store.presign_get.assert_not_awaited()


@pytest.mark.parametrize(
    "base",
    [
        "http://localhost:8080",
        "http://api.example.com",
        "https://localhost",
        "https://10.0.0.5",
        "https://api",
        "https://hail.local",
        "https://host.docker.internal",
        "https://[fd00::1]",
    ],
)
async def test_upload_refuses_a_link_reviewers_cannot_open(
    client: httpx.AsyncClient, org_and_key, store, monkeypatch, base
) -> None:
    monkeypatch.setattr("hailhq.api.routes.sms_evidence.settings.hail_api_url", base)
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"file": ("shot.png", PNG, "image/png")},
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 503
    assert "HAIL_API_URL" in resp.json()["detail"]
    store.put_attachment.assert_not_awaited()


def test_only_the_public_link_skips_the_rate_limit() -> None:
    from hailhq.api.ratelimit import _is_exempt

    assert _is_exempt(f"/v1/verifications/sms/evidence/{ORG}/{'a' * 32}")
    assert _is_exempt(f"/verifications/sms/evidence/{ORG}/{'a' * 32}")
    assert not _is_exempt("/v1/verifications/sms/evidence")
    assert not _is_exempt(f"/v1/verifications/sms/evidence/{ORG}/{'a' * 32}", "DELETE")
    assert not _is_exempt("/v1/verifications")


async def test_upload_stops_at_the_file_cap(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    from hailhq.api.routes.sms_evidence import MAX_EVIDENCE_FILES

    store.count_prefix.return_value = MAX_EVIDENCE_FILES
    resp = await client.post(
        "/v1/verifications/sms/evidence",
        files={"file": ("shot.png", PNG, "image/png")},
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 409
    assert "Delete one" in resp.json()["detail"]
    store.count_prefix.assert_awaited_once_with(
        f"sms-evidence/{org_and_key[0]}/", up_to=MAX_EVIDENCE_FILES
    )
    store.put_attachment.assert_not_awaited()


async def test_delete_removes_your_file(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    store.count_prefix.return_value = 1
    token = "c" * 32
    resp = await client.delete(
        f"/v1/verifications/sms/evidence/{org_and_key[0]}/{token}",
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 204
    store.delete.assert_awaited_once_with(f"sms-evidence/{org_and_key[0]}/{token}")


async def test_delete_never_touches_another_accounts_file(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    store.count_prefix.return_value = 1
    resp = await client.delete(
        f"/v1/verifications/sms/evidence/{ORG}/{'c' * 32}",
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 404
    store.delete.assert_not_awaited()


async def test_delete_of_a_missing_file_is_404(
    client: httpx.AsyncClient, org_and_key, store
) -> None:
    resp = await client.delete(
        f"/v1/verifications/sms/evidence/{org_and_key[0]}/{'c' * 32}",
        headers=_auth(org_and_key),
    )
    assert resp.status_code == 404
    store.delete.assert_not_awaited()


async def test_delete_needs_a_key(client: httpx.AsyncClient, store) -> None:
    resp = await client.delete(f"/v1/verifications/sms/evidence/{ORG}/{'c' * 32}")
    assert resp.status_code == 401
    store.delete.assert_not_awaited()
