"""End-to-end client tests for the `/numbers` surface."""

from __future__ import annotations

import json
from uuid import UUID, uuid4

import httpx
import pytest
import respx
from hail import Client, HailError

from tests.conftest import make_phone_number_response

# --------------------------------------------------------------------------- #
# numbers.acquire
# --------------------------------------------------------------------------- #


def _offer(quote_id, monthly=100, setup=0, readiness="ready", provider="twilio"):
    return {
        "quote_id": str(quote_id),
        "provider": provider,
        "e164": "+14155550000",
        "country_code": "US",
        "number_type": "local",
        "capabilities": ["voice", "sms"],
        "monthly_cents": monthly,
        "setup_cents": setup,
        "currency": "USD",
        "readiness": readiness,
        "requirements": [],
    }


def _quotes_response(offers):
    return httpx.Response(
        200,
        json={
            "offers": offers,
            "recommended_quote_id": None,
            "unavailable_providers": [],
            "expires_at": "2026-09-23T12:00:00Z",
        },
    )


@respx.mock
async def test_numbers_acquire_buys_the_cheapest_ready_offer(
    base_url: str, api_key: str
) -> None:
    cheap, dear, blocked = uuid4(), uuid4(), uuid4()
    quotes_route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response(
            [
                _offer(blocked, 10, 0, "verification_required"),
                _offer(dear, 200),
                _offer(cheap, 100, 50),
            ]
        )
    )
    payload = make_phone_number_response()
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.acquire(country="US", idempotency_key="idem-fixed")
    assert str(number.id) == payload["id"]
    assert number.is_dedicated is True
    assert number.capabilities == ["voice", "sms"]

    quote_body = json.loads(quotes_route.calls.last.request.content)
    assert quote_body == {
        "country_code": "US",
        "capabilities": ["voice", "sms"],
        "number_type": "local",
        "provider": "auto",
    }
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    assert req.headers["Idempotency-Key"] == "idem-fixed"
    assert json.loads(req.content) == {"country_code": "US", "quote_id": str(cheap)}


@respx.mock
async def test_numbers_acquire_passes_type_capabilities_and_provider_to_quotes(
    base_url: str, api_key: str
) -> None:
    quote_id = uuid4()
    quotes_route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response([_offer(quote_id, provider="telnyx")])
    )
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(
            country="US",
            number_type="toll_free",
            capabilities=["voice"],
            provider="telnyx",
        )
    quote_body = json.loads(quotes_route.calls.last.request.content)
    assert quote_body["number_type"] == "toll_free"
    assert quote_body["capabilities"] == ["voice"]
    assert quote_body["provider"] == "telnyx"
    assert json.loads(route.calls.last.request.content) == {
        "country_code": "US",
        "quote_id": str(quote_id),
        "number_type": "toll_free",
        "provider": "telnyx",
    }


@respx.mock
async def test_numbers_acquire_with_quote_id_skips_quotes(
    base_url: str, api_key: str
) -> None:
    quotes_route = respx.post(f"{base_url}/numbers/quotes")
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    quote_id = uuid4()
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(country="US", quote_id=quote_id, provider="twilio")
    assert not quotes_route.called
    assert json.loads(route.calls.last.request.content) == {
        "country_code": "US",
        "quote_id": str(quote_id),
        "provider": "twilio",
    }


@respx.mock
async def test_numbers_acquire_without_a_ready_offer_raises_and_does_not_buy(
    base_url: str, api_key: str
) -> None:
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response(
            [_offer(uuid4(), readiness="verification_required")]
        )
    )
    route = respx.post(f"{base_url}/numbers")
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailError, match="no number ready to buy"):
            await c.numbers.acquire(country="US")
    assert not route.called


@respx.mock
async def test_numbers_acquire_auto_generates_idempotency_key(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(country="US", quote_id=uuid4())
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


@respx.mock
async def test_live_quotes_and_explicit_carrier_purchase(base_url: str, api_key: str):
    quote_id = str(uuid4())
    quotes_route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "quote_id": quote_id,
                        "provider": "telnyx",
                        "e164": "+351211234567",
                        "country_code": "PT",
                        "number_type": "local",
                        "capabilities": ["voice"],
                        "monthly_cents": 200,
                        "setup_cents": 100,
                        "currency": "USD",
                        "readiness": "ready",
                        "requirements": [],
                    }
                ],
                "recommended_quote_id": quote_id,
                "unavailable_providers": [],
                "expires_at": "2026-09-23T12:00:00Z",
            },
        )
    )
    payload = {
        **make_phone_number_response(),
        "provider": "telnyx",
        "provisioning_state": "pending",
    }
    purchase = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        quotes = await c.numbers.quotes(
            country="pt", capabilities=["voice"], provider="telnyx"
        )
        assert str(quotes.recommended_quote_id) == quote_id
        number = await c.numbers.acquire(
            country="PT", quote_id=quotes.recommended_quote_id, provider="telnyx"
        )
        assert number.provider == "telnyx" and number.provisioning_state == "pending"
    assert json.loads(quotes_route.calls.last.request.content)["country_code"] == "PT"
    assert json.loads(purchase.calls.last.request.content)["quote_id"] == quote_id


@respx.mock
async def test_quotes_default_to_automatic_comparison(base_url: str, api_key: str):
    route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "offers": [],
                "recommended_quote_id": None,
                "unavailable_providers": [],
                "expires_at": "2026-09-23T12:00:00Z",
            },
        )
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.quotes(country="PT", capabilities=["voice"])
    assert json.loads(route.calls.last.request.content)["provider"] == "auto"


# --------------------------------------------------------------------------- #
# NumberOffer stays in sync with openapi.yaml
# --------------------------------------------------------------------------- #


def test_number_offer_matches_openapi_carrier_offer() -> None:
    from pathlib import Path

    import pytest
    from hail import NumberOffer

    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml").read_text()
    )
    schema = spec["components"]["schemas"]["CarrierOffer"]
    fields = NumberOffer.model_fields
    assert set(fields) == set(schema["properties"])
    assert {n for n, f in fields.items() if f.is_required()} == set(schema["required"])


def test_number_offer_accepts_a_minimal_and_a_full_offer() -> None:
    from hail import NumberOffer

    minimal = {
        "provider": "telnyx",
        "e164": "+351211234567",
        "country_code": "PT",
        "number_type": "local",
        "capabilities": ["voice"],
        "monthly_cents": 100,
        "setup_cents": 0,
        "readiness": "ready",
    }
    offer = NumberOffer.model_validate(minimal)
    assert offer.currency == "USD"
    assert offer.requirements == []
    assert offer.quote_id is None
    full = NumberOffer.model_validate(
        {
            **minimal,
            "verification_id": "v1",
            "address_id": "a1",
            "quote_id": str(uuid4()),
        }
    )
    assert full.verification_id == "v1"
    assert full.address_id == "a1"


@respx.mock
async def test_numbers_acquire_with_quote_sends_number_type_only_when_given(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    quote_id = uuid4()
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(country="PT", quote_id=quote_id)
        await c.numbers.acquire(country="PT", quote_id=quote_id, number_type="mobile")
    first, second = (json.loads(call.request.content) for call in route.calls)
    assert first == {"country_code": "PT", "quote_id": str(quote_id)}
    assert second["number_type"] == "mobile"
