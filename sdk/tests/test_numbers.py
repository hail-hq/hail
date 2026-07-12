"""End-to-end client tests for the `/numbers` surface."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import respx

from hail import Client
from tests.conftest import make_phone_number_response

# --------------------------------------------------------------------------- #
# numbers.acquire
# --------------------------------------------------------------------------- #


@respx.mock
async def test_numbers_acquire_happy_path(base_url: str, api_key: str) -> None:
    payload = make_phone_number_response()
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.acquire(country="US", idempotency_key="idem-fixed")
    assert str(number.id) == payload["id"]
    assert number.is_dedicated is True
    assert number.capabilities == ["voice", "sms"]

    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    assert req.headers["Idempotency-Key"] == "idem-fixed"
    body = json.loads(req.content)
    assert body == {"country_code": "US", "number_type": "local"}


@respx.mock
async def test_numbers_acquire_toll_free(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(country="US", number_type="toll_free")
    body = json.loads(route.calls.last.request.content)
    assert body["number_type"] == "toll_free"


@respx.mock
async def test_numbers_acquire_auto_generates_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(country="US")
    UUID(route.calls.last.request.headers["Idempotency-Key"])  # raises if invalid


# --------------------------------------------------------------------------- #
# numbers.get / list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_numbers_get_happy_path(base_url: str, api_key: str) -> None:
    payload = make_phone_number_response(messaging_service_sid="MGdeadbeef")
    nid = payload["id"]
    route = respx.get(f"{base_url}/numbers/{nid}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.get(nid)
    assert str(number.id) == nid
    assert number.messaging_service_sid == "MGdeadbeef"
    assert route.called


@respx.mock
async def test_numbers_list_with_pagination(base_url: str, api_key: str) -> None:
    payload = {
        "items": [make_phone_number_response(), make_phone_number_response()],
        "next_cursor": "next-page",
    }
    route = respx.get(f"{base_url}/numbers").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.numbers.list(cursor="cursorX", limit=25)
    assert len(result.items) == 2
    assert result.next_cursor == "next-page"
    qp = dict(route.calls.last.request.url.params)
    assert qp == {"cursor": "cursorX", "limit": "25"}


# --------------------------------------------------------------------------- #
# numbers.enable_sms
# --------------------------------------------------------------------------- #


@respx.mock
async def test_numbers_enable_sms_happy_path(base_url: str, api_key: str) -> None:
    nid = str(uuid4())
    payload = make_phone_number_response(
        number_id=UUID(nid), messaging_service_sid="MGnew"
    )
    route = respx.post(f"{base_url}/numbers/{nid}/enable-sms").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.enable_sms(nid)
    assert number.messaging_service_sid == "MGnew"
    assert route.called
    assert route.calls.last.request.method == "POST"
