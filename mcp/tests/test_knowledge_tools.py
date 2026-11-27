"""The knowledge base MCP tools and the agent ``knowledge`` switch."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import respx
from jsonschema import Draft202012Validator

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
_TAGS = [{"type": "brand", "value": "Example"}]


@respx.mock
async def test_add_and_get_document(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    upload_id = str(uuid4())
    add = respx.post(f"{_BASE_URL}/knowledge/documents").mock(
        return_value=httpx.Response(201, json={**_DOC, "status": "pending"})
    )
    respx.get(f"{_BASE_URL}/knowledge/documents/{_DOC_ID}").mock(
        return_value=httpx.Response(200, json={**_DOC, "tags": _TAGS})
    )
    out = await srv.mcp_app.call_tool(
        "add_knowledge_document", {"upload_id": upload_id}
    )
    assert out.structured_content["status"] == "pending"
    assert out.structured_content["tags"] == []
    assert json.loads(add.calls[0].request.read()) == {"upload_id": upload_id}
    out = await srv.mcp_app.call_tool(
        "get_knowledge_document", {"document_id": _DOC_ID}
    )
    assert out.structured_content["piece_count"] == 3
    assert out.structured_content["tags"] == _TAGS
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


@respx.mock
async def test_list_survives_a_network_error(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    respx.get(f"{_BASE_URL}/knowledge/documents").mock(
        side_effect=httpx.ConnectError("down")
    )
    out = await srv.mcp_app.call_tool("list_knowledge_documents", {})
    assert "error" in out.structured_content


async def test_knowledge_tools_publish_output_schemas(monkeypatch) -> None:
    srv = await _real_server(monkeypatch)
    tools = {t.name: t for t in await srv.mcp_app.list_tools()}
    doc = Draft202012Validator(tools["get_knowledge_document"].output_schema)
    assert doc.is_valid({**_DOC, "tags": _TAGS})
    assert not doc.is_valid({**_DOC, "tags": _TAGS, "status": "lost"})
    gone = Draft202012Validator(tools["delete_knowledge_document"].output_schema)
    assert gone.is_valid({"deleted": True, "document_id": _DOC_ID, "name": "m"})
    found = Draft202012Validator(tools["search_knowledge"].output_schema)
    assert found.is_valid({"results": [], "note": "data"})
