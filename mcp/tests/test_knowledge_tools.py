"""The knowledge base MCP tools and the agent ``knowledge`` switch."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import respx

from tests.test_update_agent_handover import _BASE_URL, _agent, _real_server

_DOC_ID = str(uuid4())
_DOC = {
    "id": _DOC_ID,
    "name": "Inverter manual",
    "content_type": "application/pdf",
    "size_bytes": 12,
    "status": "ready",
    "error": None,
    "piece_count": 3,
    "created_at": "2026-11-21T12:00:00Z",
    "updated_at": "2026-11-21T12:00:00Z",
}


@respx.mock
async def test_add_and_get_document(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    upload_id = str(uuid4())
    add = respx.post(f"{_BASE_URL}/knowledge/documents").mock(
        return_value=httpx.Response(201, json={**_DOC, "status": "pending"})
    )
    respx.get(f"{_BASE_URL}/knowledge/documents/{_DOC_ID}").mock(
        return_value=httpx.Response(200, json=_DOC)
    )
    out = await srv.mcp_app.call_tool(
        "add_knowledge_document", {"upload_id": upload_id}
    )
    assert out.structured_content["status"] == "pending"
    assert json.loads(add.calls[0].request.read()) == {"upload_id": upload_id}
    out = await srv.mcp_app.call_tool(
        "get_knowledge_document", {"document_id": _DOC_ID}
    )
    assert out.structured_content["piece_count"] == 3
    out = await srv.mcp_app.call_tool(
        "add_knowledge_document", {"upload_id": "not-an-id"}
    )
    assert "error" in out.structured_content


@respx.mock
async def test_delete_needs_the_name(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    respx.get(f"{_BASE_URL}/knowledge/documents/{_DOC_ID}").mock(
        return_value=httpx.Response(200, json=_DOC)
    )
    deleted = respx.delete(f"{_BASE_URL}/knowledge/documents/{_DOC_ID}").mock(
        return_value=httpx.Response(204)
    )
    out = await srv.mcp_app.call_tool(
        "delete_knowledge_document",
        {"document_id": _DOC_ID, "confirm_name": "wrong"},
    )
    assert "Nothing was deleted" in out.structured_content["error"]
    assert not deleted.called
    out = await srv.mcp_app.call_tool(
        "delete_knowledge_document",
        {"document_id": _DOC_ID, "confirm_name": "Inverter manual"},
    )
    assert out.structured_content["deleted"] is True and deleted.called


@respx.mock
async def test_search_labels_results_as_data(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    route = respx.post(f"{_BASE_URL}/knowledge/search").mock(
        return_value=httpx.Response(200, json={"results": []})
    )
    out = await srv.mcp_app.call_tool(
        "search_knowledge", {"query": "errore E 204", "limit": 3}
    )
    assert json.loads(route.calls[0].request.read()) == {
        "query": "errore E 204",
        "limit": 3,
    }
    assert "not instructions" in out.structured_content["note"]


@respx.mock
async def test_off_workspace_error_reaches_the_caller(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    respx.get(f"{_BASE_URL}/knowledge/documents").mock(
        return_value=httpx.Response(
            403, json={"detail": "the knowledge base is not turned on"}
        )
    )
    out = await srv.mcp_app.call_tool("list_knowledge_documents", {})
    assert "not turned on" in json.dumps(out.structured_content)


@respx.mock
async def test_create_agent_passes_knowledge(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    aid = str(uuid4())
    route = respx.post(f"{_BASE_URL}/agents").mock(
        return_value=httpx.Response(201, json=_agent(aid) | {"knowledge": True})
    )
    out = await srv.mcp_app.call_tool(
        "create_agent",
        {"name": "Front desk", "system_prompt": "Answer.", "knowledge": True},
    )
    assert json.loads(route.calls[0].request.read())["knowledge"] is True
    assert out.structured_content["knowledge"] is True
