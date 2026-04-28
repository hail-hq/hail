"""End-to-end client tests using respx-mocked httpx transports."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import httpx
import pytest
import respx

from hail import (
    Client,
    HailAuthError,
    HailConfigError,
    HailIdempotencyConflict,
    HailMalformedResourceId,
    HailNotFoundError,
    HailServerError,
    HailValidationError,
)
from hail._http import _HailHTTP
from tests.conftest import make_call_response, make_event

# --------------------------------------------------------------------------- #
# Construction
# --------------------------------------------------------------------------- #


def test_client_requires_api_key() -> None:
    with pytest.raises(HailConfigError):
        Client()


def test_client_picks_up_env_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HAIL_API_KEY", "sk-from-env")
    monkeypatch.setenv("HAIL_API_URL", "https://api.example.com")
    c = Client()
    assert c.base_url == "https://api.example.com"


# --------------------------------------------------------------------------- #
# calls.create
# --------------------------------------------------------------------------- #


@respx.mock
async def test_calls_create_happy_path_mode_a(base_url: str, api_key: str) -> None:
    payload = make_call_response()
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        call = await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            idempotency_key="idem-fixed",
        )
    assert str(call.id) == payload["id"]
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    assert req.headers["Idempotency-Key"] == "idem-fixed"
    body = json.loads(req.content)
    assert body == {"to": "+15555550123", "system_prompt": "be polite"}


@respx.mock
async def test_calls_create_auto_generates_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.calls.create(to="+15555550123", system_prompt="be polite")
    raw = route.calls.last.request.headers["Idempotency-Key"]
    UUID(raw)  # raises if not a valid UUID


@respx.mock
async def test_calls_create_propagates_explicit_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.calls.create(
            to="+15555550123",
            system_prompt="be polite",
            idempotency_key="caller-supplied",
        )
    assert route.calls.last.request.headers["Idempotency-Key"] == "caller-supplied"


@respx.mock
async def test_calls_create_with_llm_block(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.calls.create(
            to="+15555550123",
            llm={
                "base_url": "https://api.openai.com/v1",
                "api_key": "sk-x",
                "model": "gpt-4o-mini",
            },
        )
    body = json.loads(route.calls.last.request.content)
    assert body["llm"]["model"] == "gpt-4o-mini"
    assert "system_prompt" not in body


# --------------------------------------------------------------------------- #
# calls.get / list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_calls_get_happy_path(base_url: str, api_key: str) -> None:
    payload = make_call_response()
    cid = payload["id"]
    route = respx.get(f"{base_url}/calls/{cid}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        call = await c.calls.get(cid)
    assert str(call.id) == cid
    assert route.called


@respx.mock
async def test_calls_list_with_filters(base_url: str, api_key: str) -> None:
    payload = {"items": [make_call_response()], "next_cursor": None}
    route = respx.get(f"{base_url}/calls").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.calls.list(
            cursor="cursorX",
            limit=25,
            status="completed",
            to="+15555550123",
        )
    assert len(result.items) == 1
    qp = dict(route.calls.last.request.url.params)
    assert qp == {
        "cursor": "cursorX",
        "limit": "25",
        "status": "completed",
        "to": "+15555550123",
    }


# --------------------------------------------------------------------------- #
# events.list / tail
# --------------------------------------------------------------------------- #


@respx.mock
async def test_events_list_with_id_filter(base_url: str, api_key: str) -> None:
    cid = uuid4()
    payload = {"items": [], "next_cursor": None, "call_status": "queued"}
    route = respx.get(f"{base_url}/events").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.events.list(id=f"call:{cid}")
    assert route.calls.last.request.url.params["id"] == f"call:{cid}"


@respx.mock
async def test_events_tail_yields_then_terminates(base_url: str, api_key: str) -> None:
    cid = uuid4()
    t0 = datetime.now(timezone.utc)
    page1 = {
        "items": [
            make_event(call_id=cid, kind="state_change", occurred_at=t0),
            make_event(
                call_id=cid, kind="agent_turn", occurred_at=t0 + timedelta(seconds=1)
            ),
        ],
        "next_cursor": None,
        "call_status": "in_progress",
    }
    page2 = {
        "items": [
            make_event(
                call_id=cid, kind="state_change", occurred_at=t0 + timedelta(seconds=2)
            ),
        ],
        "next_cursor": None,
        "call_status": "completed",
    }
    respx.get(f"{base_url}/events").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    seen = []
    async with Client(api_key=api_key, base_url=base_url) as c:
        async for ev in c.events.tail(id=f"call:{cid}", interval_seconds=0):
            seen.append(ev)
    assert len(seen) == 3
    assert {e.kind for e in seen} == {"state_change", "agent_turn"}


@respx.mock
async def test_events_tail_no_follow_stops_after_one_page(
    base_url: str, api_key: str
) -> None:
    cid = uuid4()
    payload = {
        "items": [make_event(call_id=cid)],
        "next_cursor": None,
        "call_status": "in_progress",
    }
    route = respx.get(f"{base_url}/events").mock(
        return_value=httpx.Response(200, json=payload)
    )
    seen = []
    async with Client(api_key=api_key, base_url=base_url) as c:
        async for ev in c.events.tail(id=f"call:{cid}", follow=False):
            seen.append(ev)
    assert len(seen) == 1
    assert route.call_count == 1


@respx.mock
async def test_events_tail_synthesizes_cursor(base_url: str, api_key: str) -> None:
    """When the server didn't set next_cursor, poll N+1 carries a cursor
    derived from the last event of poll N."""
    cid = uuid4()
    last_id = uuid4()
    last_ts = datetime(2026, 4, 22, 10, 30, 0, tzinfo=timezone.utc)
    page1 = {
        "items": [
            make_event(
                call_id=cid,
                event_id=last_id,
                occurred_at=last_ts,
            ),
        ],
        "next_cursor": None,
        "call_status": "in_progress",
    }
    page2 = {
        "items": [],
        "next_cursor": None,
        "call_status": "completed",
    }
    route = respx.get(f"{base_url}/events").mock(
        side_effect=[
            httpx.Response(200, json=page1),
            httpx.Response(200, json=page2),
        ]
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        async for _ in c.events.tail(id=f"call:{cid}", interval_seconds=0):
            pass

    # Second request must carry a cursor; first one must not.
    first_qp = dict(route.calls[0].request.url.params)
    second_qp = dict(route.calls[1].request.url.params)
    assert "cursor" not in first_qp
    assert "cursor" in second_qp

    # Decode and assert the synthesized cursor matches (last_ts, last_id).
    import base64

    raw = second_qp["cursor"] + "=" * (-len(second_qp["cursor"]) % 4)
    decoded = base64.urlsafe_b64decode(raw.encode("ascii")).decode("utf-8")
    ts_str, id_str = decoded.split("|", 1)
    assert datetime.fromisoformat(ts_str) == last_ts
    assert id_str == str(last_id)


async def test_events_tail_rejects_malformed_id(base_url: str, api_key: str) -> None:
    """Local-validation must fire BEFORE any HTTP — no respx route needed."""
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailMalformedResourceId):
            agen = c.events.tail(id="not-typed")
            await agen.__anext__()


# --------------------------------------------------------------------------- #
# HTTP retries + error mapping
# --------------------------------------------------------------------------- #


async def _no_sleep(_seconds: float) -> None:
    return None


@respx.mock
async def test_http_retries_5xx_then_succeeds(base_url: str, api_key: str) -> None:
    payload = make_call_response()
    cid = payload["id"]
    route = respx.get(f"{base_url}/calls/{cid}").mock(
        side_effect=[
            httpx.Response(503, json={"detail": "try again"}),
            httpx.Response(200, json=payload),
        ]
    )
    # Build the http wrapper directly so we can stub sleep.
    http = _HailHTTP(base_url=base_url, api_key=api_key, sleep=_no_sleep)
    try:
        data = await http.request("GET", f"/calls/{cid}")
    finally:
        await http.aclose()
    assert data["id"] == cid
    assert route.call_count == 2


@respx.mock
async def test_http_does_not_retry_non_idempotent_without_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(503, json={"detail": "boom"})
    )
    http = _HailHTTP(base_url=base_url, api_key=api_key, sleep=_no_sleep)
    try:
        with pytest.raises(HailServerError):
            # No Idempotency-Key header -> not retried.
            await http.request("POST", "/calls", json={"to": "+15550001111"})
    finally:
        await http.aclose()
    assert route.call_count == 1


@respx.mock
async def test_http_retries_post_with_idempotency_key(
    base_url: str, api_key: str
) -> None:
    """Sanity check: POST + Idempotency-Key IS retried on 5xx."""
    payload = make_call_response()
    route = respx.post(f"{base_url}/calls").mock(
        side_effect=[
            httpx.Response(503, json={"detail": "boom"}),
            httpx.Response(201, json=payload),
        ]
    )
    http = _HailHTTP(base_url=base_url, api_key=api_key, sleep=_no_sleep)
    try:
        data = await http.request(
            "POST",
            "/calls",
            json={"to": "+15555550123", "system_prompt": "x"},
            headers={"Idempotency-Key": "abc"},
        )
    finally:
        await http.aclose()
    assert data["id"] == payload["id"]
    assert route.call_count == 2


@respx.mock
async def test_error_mapping_401(base_url: str, api_key: str) -> None:
    respx.get(f"{base_url}/calls").mock(
        return_value=httpx.Response(401, json={"detail": "bad key"})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailAuthError) as exc:
            await c.calls.list()
    assert exc.value.status_code == 401


@respx.mock
async def test_error_mapping_404(base_url: str, api_key: str) -> None:
    cid = uuid4()
    respx.get(f"{base_url}/calls/{cid}").mock(
        return_value=httpx.Response(404, json={"detail": "call not found"})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailNotFoundError) as exc:
            await c.calls.get(cid)
    assert exc.value.status_code == 404


@respx.mock
async def test_error_mapping_422(base_url: str, api_key: str) -> None:
    detail = [{"loc": ["body", "to"], "msg": "must be E.164", "type": "value_error"}]
    respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(422, json={"detail": detail})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailValidationError) as exc:
            await c.calls.create(to="+15555550123", system_prompt="hi")
    assert exc.value.status_code == 422
    assert exc.value.detail == detail


@respx.mock
async def test_error_mapping_409(base_url: str, api_key: str) -> None:
    respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(409, json={"detail": "idempotency conflict"})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailIdempotencyConflict) as exc:
            await c.calls.create(to="+15555550123", system_prompt="hi")
    assert exc.value.status_code == 409


@respx.mock
async def test_error_mapping_500(base_url: str, api_key: str) -> None:
    """5xx after retry-budget exhaustion surfaces as HailServerError."""
    cid = uuid4()
    respx.get(f"{base_url}/calls/{cid}").mock(
        return_value=httpx.Response(500, json={"detail": "boom"})
    )
    http = _HailHTTP(base_url=base_url, api_key=api_key, sleep=_no_sleep)
    try:
        with pytest.raises(HailServerError) as exc:
            await http.request("GET", f"/calls/{cid}")
    finally:
        await http.aclose()
    assert exc.value.status_code == 500
