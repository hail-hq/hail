"""``client.knowledge``: add, list, get, delete and search documents."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import respx
from hail import Client

_DOC = {
    "id": str(uuid4()),
    "name": "Inverter manual",
    "content_type": "application/pdf",
    "size_bytes": 12,
    "status": "pending",
    "error": None,
    "piece_count": 0,
    "created_at": "2026-11-21T12:00:00Z",
    "updated_at": "2026-11-21T12:00:00Z",
}


@respx.mock
async def test_add_document_uploads_then_adds(base_url: str, api_key: str) -> None:
    upload_id = str(uuid4())
    upload_url = f"https://uploads.example.test/v1/uploads/{upload_id}?token=t"
    link = respx.post(f"{base_url}/uploads").mock(
        return_value=httpx.Response(
            201, json={"id": upload_id, "upload_url": upload_url}
        )
    )
    put = respx.put(upload_url).mock(
        return_value=httpx.Response(200, json={"id": upload_id})
    )
    add = respx.post(f"{base_url}/knowledge/documents").mock(
        return_value=httpx.Response(201, json=_DOC)
    )
    async with Client(api_key=api_key, base_url=base_url) as client:
        doc = await client.knowledge.add_document(
            filename="manual.pdf",
            content=b"%PDF-1.4 x",
            content_type="application/pdf",
            name="Inverter manual",
        )
    assert doc.status == "pending"
    assert json.loads(link.calls[0].request.content)["purpose"] == "knowledge_document"
    assert "authorization" not in put.calls[0].request.headers
    assert put.calls[0].request.content == b"%PDF-1.4 x"
    assert json.loads(add.calls[0].request.content) == {
        "upload_id": upload_id,
        "name": "Inverter manual",
    }


@respx.mock
async def test_list_get_delete_search(base_url: str, api_key: str) -> None:
    respx.get(f"{base_url}/knowledge/documents").mock(
        return_value=httpx.Response(200, json={"items": [_DOC], "next_cursor": None})
    )
    respx.get(f"{base_url}/knowledge/documents/{_DOC['id']}").mock(
        return_value=httpx.Response(200, json={**_DOC, "status": "ready"})
    )
    deleted = respx.delete(f"{base_url}/knowledge/documents/{_DOC['id']}").mock(
        return_value=httpx.Response(204)
    )
    search = respx.post(f"{base_url}/knowledge/search").mock(
        return_value=httpx.Response(
            200,
            json={
                "results": [
                    {
                        "document_id": _DOC["id"],
                        "document_name": "Inverter manual",
                        "text": "E-204 means grid voltage out of range.",
                        "page": 4,
                        "row": None,
                        "score": 0.03,
                    }
                ]
            },
        )
    )
    async with Client(api_key=api_key, base_url=base_url) as client:
        assert (await client.knowledge.list_documents()).items[
            0
        ].name == "Inverter manual"
        assert (await client.knowledge.get_document(_DOC["id"])).status == "ready"
        await client.knowledge.delete_document(_DOC["id"])
        hits = await client.knowledge.search("errore E 204", limit=3)
    assert deleted.called
    assert json.loads(search.calls[0].request.content) == {
        "query": "errore E 204",
        "limit": 3,
    }
    assert hits.results[0].page == 4
