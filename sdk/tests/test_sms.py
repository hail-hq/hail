"""End-to-end client tests for the `/sms` surface."""

from __future__ import annotations

import json
from uuid import UUID

import httpx
import pytest
import respx

from hail import Client, HailNotFoundError
from tests.conftest import make_sms_response, make_suppression_response

# --------------------------------------------------------------------------- #
# sms.create
# --------------------------------------------------------------------------- #


@respx.mock
async def test_sms_create_happy_path(base_url: str, api_key: str) -> None:
    payload = make_sms_response()
    route = respx.post(f"{base_url}/sms").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sms = await c.sms.create(
            to="+15555550123",
            body="hi",
            recipient_consent=True,
            idempotency_key="idem-fixed",
        )
    assert str(sms.id) == payload["id"]
    assert sms.status == "sent"
    assert sms.from_e164 == "+15550001111"

    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    assert req.headers["Idempotency-Key"] == "idem-fixed"
    body = json.loads(req.content)
    assert body == {
        "to": "+15555550123",
        "body": "hi",
        "recipient_consent": True,
    }


@respx.mock
async def test_sms_create_sends_consent_fields(base_url: str, api_key: str) -> None:
    route = respx.post(f"{base_url}/sms").mock(
        return_value=httpx.Response(201, json=make_sms_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.sms.create(
            to="+15555550123",
            body="hi",
            recipient_consent=True,
            consent_source="signup_form",
            message_type="marketing",
        )
    body = json.loads(route.calls.last.request.content)
    assert body["recipient_consent"] is True
    assert body["consent_source"] == "signup_form"
    assert body["message_type"] == "marketing"


@respx.mock
async def test_sms_create_omits_optional_fields_when_not_passed(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/sms").mock(
        return_value=httpx.Response(201, json=make_sms_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.sms.create(to="+15555550123", body="hi", recipient_consent=True)
    body = json.loads(route.calls.last.request.content)
    assert body == {
        "to": "+15555550123",
        "body": "hi",
        "recipient_consent": True,
    }


@respx.mock
async def test_sms_create_auto_generates_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/sms").mock(
        return_value=httpx.Response(201, json=make_sms_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.sms.create(to="+15555550123", body="hi", recipient_consent=True)
    raw = route.calls.last.request.headers["Idempotency-Key"]
    UUID(raw)  # raises if not a valid UUID


@respx.mock
async def test_sms_create_propagates_explicit_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/sms").mock(
        return_value=httpx.Response(201, json=make_sms_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.sms.create(
            to="+15555550123",
            body="hi",
            recipient_consent=True,
            idempotency_key="caller-supplied",
        )
    assert route.calls.last.request.headers["Idempotency-Key"] == "caller-supplied"


# --------------------------------------------------------------------------- #
# sms.get / list
# --------------------------------------------------------------------------- #


@respx.mock
async def test_sms_get_happy_path(base_url: str, api_key: str) -> None:
    payload = make_sms_response()
    sid = payload["id"]
    route = respx.get(f"{base_url}/sms/{sid}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        sms = await c.sms.get(sid)
    assert str(sms.id) == sid
    assert sms.to_e164 == "+15555550123"
    assert route.called


@respx.mock
async def test_sms_list_with_filters(base_url: str, api_key: str) -> None:
    payload = {"items": [make_sms_response()], "next_cursor": None}
    route = respx.get(f"{base_url}/sms").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.sms.list(
            cursor="cursorX",
            limit=25,
            status="delivered",
            to="+15555550123",
        )
    assert len(result.items) == 1
    qp = dict(route.calls.last.request.url.params)
    assert qp == {
        "cursor": "cursorX",
        "limit": "25",
        "status": "delivered",
        "to": "+15555550123",
    }


# --------------------------------------------------------------------------- #
# sms.list_suppressions / sms.delete_suppression
# --------------------------------------------------------------------------- #


@respx.mock
async def test_sms_list_suppressions_happy_path(base_url: str, api_key: str) -> None:
    payload = {
        "items": [make_suppression_response(), make_suppression_response()],
        "next_cursor": None,
    }
    route = respx.get(f"{base_url}/sms/suppressions").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.sms.list_suppressions()
    assert len(result.items) == 2
    assert result.items[0].recipient == "+15555550123"
    assert result.items[0].channel == "sms"
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    qp = dict(req.url.params)
    assert qp == {"limit": "50"}


@respx.mock
async def test_sms_list_suppressions_with_cursor(base_url: str, api_key: str) -> None:
    payload = {"items": [make_suppression_response()], "next_cursor": "next-page"}
    route = respx.get(f"{base_url}/sms/suppressions").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.sms.list_suppressions(cursor="cursorX", limit=10)
    assert result.next_cursor == "next-page"
    qp = dict(route.calls.last.request.url.params)
    assert qp == {"cursor": "cursorX", "limit": "10"}


@respx.mock
async def test_sms_delete_suppression_happy_path(base_url: str, api_key: str) -> None:
    route = respx.delete(f"{base_url}/sms/suppressions/+15555550123").mock(
        return_value=httpx.Response(204)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.sms.delete_suppression("+15555550123")
    assert result is None
    assert route.called
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"


@respx.mock
async def test_sms_delete_suppression_not_found_raises(
    base_url: str, api_key: str
) -> None:
    respx.delete(f"{base_url}/sms/suppressions/+15555550123").mock(
        return_value=httpx.Response(404, json={"detail": "suppression not found"})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailNotFoundError) as exc:
            await c.sms.delete_suppression("+15555550123")
    assert "suppression not found" in str(exc.value)
