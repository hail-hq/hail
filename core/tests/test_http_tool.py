"""Customer HTTP tools at run time: signing, SSRF refusal, timeouts and the
text the model gets back. A local aiohttp server stands in for the
customer's API; its loopback address is allowed only where a test says so."""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest
from aiohttp import web
from hailhq.core.agent_tools import http_tool
from hailhq.core.agent_tools.http_tool import (
    BLOCKED,
    Conversation,
    HttpTool,
    PublicOnlyResolver,
    call_http_tool,
    tool_spec,
)
from hailhq.core.webhooks import sign_payload

SECRET = "hts_test_secret"


def _conv(channel="call") -> Conversation:
    return Conversation(
        channel=channel,
        id=uuid.uuid4(),
        agent_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
    )


def _tool(url: str, **over) -> HttpTool:
    base = {
        "name": "lookup_order",
        "description": "Find an order.",
        "url": url,
        "parameters": {"type": "object", "properties": {}},
        "headers": {"Authorization": "Bearer customer-token"},
        "timeout_seconds": 2,
        "wait_message": None,
        "secret": SECRET,
    }
    base.update(over)
    return HttpTool(**base)


@pytest.fixture
async def server():
    """Yields (base_url, seen requests, routes to set handlers on)."""
    seen: list[dict] = []
    handlers: dict = {}

    async def handle(request: web.Request) -> web.StreamResponse:
        body = await request.read()
        seen.append({"headers": dict(request.headers), "body": body})
        return await handlers[request.path](request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handle)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]
    try:
        yield f"http://127.0.0.1:{port}", seen, handlers
    finally:
        await runner.cleanup()


@pytest.fixture
def allow_loopback(monkeypatch):
    monkeypatch.setattr(http_tool, "ip_is_public", lambda _ip: True)


async def test_request_is_signed_like_a_webhook(server, allow_loopback) -> None:
    base, seen, handlers = server

    async def ok(_req):
        return web.json_response({"status": "shipped", "eta": "Tuesday"})

    handlers["/orders"] = ok
    conv = _conv()
    out = await call_http_tool(_tool(f"{base}/orders"), conv, {"order_number": "A1"})
    assert json.loads(out) == {"status": "shipped", "eta": "Tuesday"}

    (req,) = seen
    body = req["body"]
    header = req["headers"]["X-Hail-Signature"]
    ts = header.split(",")[0].removeprefix("t=")
    assert header == sign_payload(body, SECRET, timestamp=int(ts))
    payload = json.loads(body)
    assert payload["type"] == "tool.call"
    assert payload["tool"] == "lookup_order"
    assert payload["arguments"] == {"order_number": "A1"}
    assert payload["agent_id"] == str(conv.agent_id)
    assert payload["organization_id"] == str(conv.organization_id)
    assert payload["channel"] == "call"
    assert payload["call_id"] == str(conv.id)
    assert payload["sms_id"] is None and payload["email_id"] is None
    assert req["headers"]["X-Hail-Request"] == payload["id"]
    assert req["headers"]["X-Hail-Tool"] == "lookup_order"
    assert req["headers"]["Authorization"] == "Bearer customer-token"
    assert req["headers"]["Content-Type"] == "application/json"


async def test_text_answer_is_truncated(server, allow_loopback) -> None:
    base, _seen, handlers = server

    async def big(_req):
        return web.Response(text="x" * 100_000)

    handlers["/big"] = big
    out = await call_http_tool(_tool(f"{base}/big"), _conv("sms"), {})
    assert out.endswith(" [truncated]")
    assert len(out) == http_tool.MODEL_CAP_CHARS + len(" [truncated]")


async def test_error_status_and_empty_body(server, allow_loopback) -> None:
    base, _seen, handlers = server

    async def missing(_req):
        return web.json_response({"error": "no such order"}, status=404)

    async def empty(_req):
        return web.Response(status=204)

    async def redirect(_req):
        raise web.HTTPFound("https://example.com/")

    handlers["/missing"] = missing
    handlers["/empty"] = empty
    handlers["/redirect"] = redirect
    out = await call_http_tool(_tool(f"{base}/missing"), _conv(), {})
    assert out == 'That tool failed with HTTP 404: {"error":"no such order"}'
    assert await call_http_tool(_tool(f"{base}/empty"), _conv(), {}) == (
        http_tool.NO_CONTENT
    )
    # Redirects are not followed: a public URL cannot bounce to a private one.
    out = await call_http_tool(_tool(f"{base}/redirect"), _conv(), {})
    assert out.startswith("That tool failed with HTTP 302")


async def test_timeout_returns_a_sentence(server, allow_loopback) -> None:
    base, _seen, handlers = server

    async def slow(_req):
        await asyncio.sleep(2)
        return web.Response(text="late")

    handlers["/slow"] = slow
    out = await call_http_tool(_tool(f"{base}/slow", timeout_seconds=1), _conv(), {})
    assert out == "That tool did not answer within 1 seconds."


async def test_unreachable_returns_a_sentence(allow_loopback) -> None:
    out = await call_http_tool(_tool("http://127.0.0.1:1/x"), _conv(), {})
    assert out == http_tool.UNREACHABLE


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/x",
        "https://10.0.0.5/x",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/x",
        "https://[::ffff:127.0.0.1]/x",
        "https://100.64.0.1/x",
        "https://127.1/x",
        "https://2130706433/x",
        "https://0x7f.1/x",
    ],
)
async def test_private_ip_literal_is_refused(url) -> None:
    assert await call_http_tool(_tool(url), _conv(), {}) == BLOCKED


async def test_url_that_cannot_be_parsed_never_raises() -> None:
    out = await call_http_tool(_tool("https://example.com:99999/x"), _conv(), {})
    assert out == http_tool.UNREACHABLE


async def test_host_resolving_to_loopback_is_refused(server) -> None:
    """localhost resolves to loopback: the resolver refuses it at connect
    time, so a name that changes its answer after a save is still caught."""
    base, seen, handlers = server
    port = base.rsplit(":", 1)[1]

    async def ok(_req):
        return web.Response(text="must not be reached")

    handlers["/x"] = ok
    out = await call_http_tool(_tool(f"http://localhost:{port}/x"), _conv(), {})
    assert out == BLOCKED
    assert seen == []


async def test_resolver_refuses_any_private_answer(monkeypatch) -> None:
    resolver = PublicOnlyResolver()

    async def fake_resolve(host, port=0, family=0):
        return [
            {"hostname": host, "host": "93.184.216.34", "port": port},
            {"hostname": host, "host": "10.1.2.3", "port": port},
        ]

    monkeypatch.setattr(resolver._inner, "resolve", fake_resolve)
    with pytest.raises(http_tool.BlockedAddressError):
        await resolver.resolve("rebind.example", 443)
    await resolver.close()


async def test_an_unreadable_resolved_address_is_blocked(monkeypatch, caplog) -> None:
    """An address ipaddress cannot read counts as not public, as in the
    save check (url_guard.assert_public_host)."""

    async def fake_resolve(self, host, port=0, family=0):
        return [{"hostname": host, "host": "not-an-ip", "port": port}]

    monkeypatch.setattr(http_tool.DefaultResolver, "resolve", fake_resolve)
    with caplog.at_level("WARNING", logger="hailhq.core.agent_tools"):
        out = await call_http_tool(_tool("https://odd.example/x"), _conv(), {})
    assert out == BLOCKED
    assert "unreadable address 'not-an-ip'" in caplog.text


async def test_spec_carries_the_wait_message_and_never_raises(monkeypatch) -> None:
    spec = tool_spec(_tool("https://127.0.0.1/x", wait_message="One moment."), _conv())
    assert spec.name == "lookup_order"
    assert spec.wait_message == "One moment."
    assert spec.risk_tier == "customer_api"
    assert await spec.execute(None, {}) == BLOCKED  # type: ignore[arg-type]
