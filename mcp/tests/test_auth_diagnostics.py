"""Unauthenticated request diagnostics must not expose or alter the request."""

import json

import httpx
import pytest
from hailhq.core.telemetry import PrivateSpanExporter
from hailhq.mcp import discovery_auth
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter


async def _request(*, method="POST", content=b"{}", headers=None, status=401):
    seen = {}

    async def downstream(scope, receive, send):
        seen["headers"] = scope["headers"]
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body", False):
                break
        seen["body"] = b"".join(chunks)
        await send({"type": "http.response.start", "status": status, "headers": []})
        await send({"type": "http.response.body", "body": b"unchanged"})

    provider = TracerProvider()
    memory = InMemorySpanExporter()
    provider.add_span_processor(
        SimpleSpanProcessor(PrivateSpanExporter(memory, {"service.name": "hail-mcp"}))
    )
    try:
        with provider.get_tracer(__name__).start_as_current_span("request"):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=discovery_auth.DiscoveryAuthMiddleware(downstream)
                ),
                base_url="http://test",
            ) as client:
                response = await client.request(
                    method, "/", content=content, headers=headers
                )
        exported = memory.get_finished_spans()
        assert len(exported) == 1
        return dict(exported[0].attributes), seen, response
    finally:
        provider.shutdown()


@pytest.mark.asyncio
async def test_protected_method_labels_without_payload_or_auth_change():
    payload = json.dumps(
        {"method": "tools/call", "params": {"arguments": {"secret": "private-value"}}}
    ).encode()
    attrs, seen, response = await _request(content=payload)
    assert attrs["mcp.request.method"] == "tools/call"
    assert attrs["mcp.auth.header_present"] is False
    assert attrs["mcp.auth.rejection_reason"] == "protected_method"
    assert "private-value" not in repr(attrs)
    assert seen["body"] == payload
    assert not any(key == b"authorization" for key, _ in seen["headers"])
    assert response.status_code == 401
    assert response.content == b"unchanged"


@pytest.mark.asyncio
async def test_missing_session_preserves_the_discovery_method():
    attrs, _, _ = await _request(content=b'{"method":"tools/list"}')
    assert attrs["mcp.request.method"] == "tools/list"
    assert attrs["mcp.auth.rejection_reason"] == "missing_session"


@pytest.mark.asyncio
async def test_rate_limited_initialize_is_identifiable(monkeypatch):
    monkeypatch.setattr(discovery_auth, "_anon_init_cap_exceeded", lambda _: True)
    attrs, seen, _ = await _request(content=b'{"method":"initialize"}')
    assert attrs["mcp.request.method"] == "initialize"
    assert attrs["mcp.auth.rejection_reason"] == "discovery_rate_limited"
    assert not any(key == b"authorization" for key, _ in seen["headers"])


@pytest.mark.asyncio
async def test_successful_anonymous_discovery_has_no_rejection_reason():
    attrs, seen, response = await _request(
        content=b'{"method":"initialize"}', status=200
    )
    assert attrs["mcp.auth.discovery"] is True
    assert attrs["mcp.auth.header_present"] is False
    assert "mcp.auth.rejection_reason" not in attrs
    assert (b"authorization", b"Bearer anonymous-discovery") in seen["headers"]
    assert response.status_code == 200


@pytest.mark.asyncio
async def test_credentials_are_forwarded_without_peeking_or_recording_them():
    payload = b'{"method":"tools/call","params":{"secret":"private-value"}}'
    attrs, seen, _ = await _request(
        content=payload, headers={"Authorization": "Bearer private-credential"}
    )
    assert attrs["mcp.auth.header_present"] is True
    assert attrs["mcp.request.method"] == "uninspected"
    assert attrs["mcp.auth.rejection_reason"] == "credentials_rejected"
    assert "private-credential" not in repr(attrs)
    assert "private-value" not in repr(attrs)
    assert (b"authorization", b"Bearer private-credential") in seen["headers"]
    assert seen["body"] == payload


@pytest.mark.asyncio
@pytest.mark.parametrize("content", [b"{bad-json", b'[{"method":"initialize"}]', b"{}"])
async def test_invalid_envelope_is_bounded_and_not_recorded(content):
    attrs, seen, _ = await _request(content=content)
    assert attrs["mcp.request.method"] == "unknown"
    assert attrs["mcp.auth.rejection_reason"] == "invalid_request"
    assert seen["body"] == content


@pytest.mark.asyncio
async def test_unknown_method_value_cannot_become_telemetry_content():
    attrs, _, _ = await _request(content=b'{"method":"Bearer private-credential"}')
    assert attrs["mcp.request.method"] == "other"
    assert attrs["mcp.auth.rejection_reason"] == "protected_method"
    assert "private-credential" not in repr(attrs)


@pytest.mark.asyncio
async def test_oversized_body_labels_the_existing_rejection(monkeypatch):
    monkeypatch.setattr(discovery_auth, "_MAX_PEEK_BYTES", 8)
    payload = b'{"method":"initialize"}'
    attrs, seen, _ = await _request(content=payload)
    assert attrs["mcp.auth.rejection_reason"] == "body_too_large"
    assert attrs["mcp.request.method"] == "uninspected"
    assert seen["body"] == payload


@pytest.mark.asyncio
async def test_delete_rejection_distinguishes_missing_credentials():
    attrs, _, _ = await _request(method="DELETE")
    assert attrs["mcp.auth.header_present"] is False
    assert attrs["mcp.auth.rejection_reason"] == "missing_authorization"


@pytest.mark.asyncio
async def test_body_peek_timeout_does_not_consume_the_body(monkeypatch):
    monkeypatch.setattr(discovery_auth, "_MAX_PEEK_SECONDS", 0)
    payload = b'{"method":"initialize"}'
    attrs, seen, _ = await _request(content=payload)
    assert attrs["mcp.auth.rejection_reason"] == "body_timeout"
    assert attrs["mcp.request.method"] == "uninspected"
    assert seen["body"] == payload


@pytest.mark.asyncio
async def test_forwarded_credentials_are_not_labelled_rejected_on_success():
    attrs, _, response = await _request(
        headers={"Authorization": "Bearer secret"}, status=200
    )
    assert attrs["mcp.auth.header_present"] is True
    assert "mcp.auth.rejection_reason" not in attrs
    assert response.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method", ["server/discover", "tools/list", "notifications/initialized"]
)
async def test_single_exchange_discovery_is_not_labelled_missing_session(method):
    attrs, seen, response = await _request(
        content=json.dumps({"method": method}).encode(),
        headers={"MCP-Protocol-Version": "2026-07-28"},
        status=200,
    )
    assert attrs["mcp.request.method"] == method
    assert attrs["mcp.auth.discovery"] is True
    assert "mcp.auth.rejection_reason" not in attrs
    assert (b"authorization", b"Bearer anonymous-discovery") in seen["headers"]
    assert response.status_code == 200
