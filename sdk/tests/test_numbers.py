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


def _offer(quote_id, monthly=100, setup=0, readiness="ready"):
    return {
        "quote_id": str(quote_id),
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
            "incomplete": False,
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
    }
    req = route.calls.last.request
    assert req.headers["Authorization"] == f"Bearer {api_key}"
    assert req.headers["Idempotency-Key"] == "idem-fixed"
    assert json.loads(req.content) == {"country_code": "US", "quote_id": str(cheap)}


@respx.mock
async def test_numbers_acquire_never_buys_a_by_request_offer(
    base_url: str, api_key: str
) -> None:
    asked, real = uuid4(), uuid4()
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response(
            [
                {**_offer(asked, 10), "e164": None, "by_request": True},
                _offer(real, 100),
            ]
        )
    )
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(country="US", idempotency_key="idem-fixed")
    assert json.loads(route.calls.last.request.content)["quote_id"] == str(real)


@respx.mock
async def test_numbers_acquire_passes_type_and_capabilities_to_quotes(
    base_url: str, api_key: str
) -> None:
    quote_id = uuid4()
    quotes_route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response([_offer(quote_id)])
    )
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(
            country="US",
            number_type="toll_free",
            capabilities=["voice"],
            area_code="480",
            city="Phoenix",
            region="AZ",
        )
    quote_body = json.loads(quotes_route.calls.last.request.content)
    assert quote_body["area_code"] == "480"
    assert (quote_body["city"], quote_body["region"]) == ("Phoenix", "AZ")
    assert quote_body["number_type"] == "toll_free"
    assert quote_body["capabilities"] == ["voice"]
    # The SDK never sends a carrier: Hail picks it.
    assert "provider" not in quote_body
    assert json.loads(route.calls.last.request.content) == {
        "country_code": "US",
        "quote_id": str(quote_id),
        "number_type": "toll_free",
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
        await c.numbers.acquire(country="US", quote_id=quote_id)
    assert not quotes_route.called
    assert json.loads(route.calls.last.request.content) == {
        "country_code": "US",
        "quote_id": str(quote_id),
    }


@respx.mock
async def test_numbers_acquire_sends_expected_total_cents_only_when_set(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=make_phone_number_response())
    )
    quote_id = uuid4()
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.acquire(
            country="US", quote_id=quote_id, expected_total_cents=165
        )
        assert json.loads(route.calls.last.request.content) == {
            "country_code": "US",
            "quote_id": str(quote_id),
            "expected_total_cents": 165,
        }
        await c.numbers.acquire(country="US", quote_id=quote_id)
        assert "expected_total_cents" not in json.loads(
            route.calls.last.request.content
        )


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
async def test_numbers_acquire_says_to_ask_support_when_every_offer_is_by_request(
    base_url: str, api_key: str
) -> None:
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response(
            [
                {
                    **_offer(uuid4(), readiness="verification_required"),
                    "e164": None,
                    "by_request": True,
                },
                {**_offer(uuid4(), 10), "e164": None, "by_request": True},
            ]
        )
    )
    route = respx.post(f"{base_url}/numbers")
    async with Client(api_key=api_key, base_url=base_url) as c:
        with pytest.raises(HailError, match="set up by our team.*ask support"):
            await c.numbers.acquire(country="DE", number_type="local")
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
    payload = make_phone_number_response()
    nid = payload["id"]
    route = respx.get(f"{base_url}/numbers/{nid}").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.get(nid)
    assert str(number.id) == nid
    assert not hasattr(number, "messaging_service_sid")
    assert not hasattr(number, "provider")
    assert route.called


@respx.mock
async def test_numbers_regions(base_url: str, api_key: str) -> None:
    region = {"country_code": "CA", "code": "ON", "name": "Ontario"}
    respx.get(f"{base_url}/numbers/regions").mock(
        return_value=httpx.Response(200, json={"regions": [region]})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        result = await c.numbers.regions()
    assert result.regions[0].code == "ON"


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
    payload = make_phone_number_response(number_id=UUID(nid))
    route = respx.post(f"{base_url}/numbers/{nid}/enable-sms").mock(
        return_value=httpx.Response(200, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.enable_sms(nid)
    assert str(number.id) == nid
    assert route.called
    assert route.calls.last.request.method == "POST"


@respx.mock
async def test_live_quotes_and_purchase(base_url: str, api_key: str):
    quote_id = str(uuid4())
    quotes_route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "quote_id": quote_id,
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
                "incomplete": False,
                "expires_at": "2026-09-23T12:00:00Z",
            },
        )
    )
    payload = {**make_phone_number_response(), "provisioning_state": "pending"}
    purchase = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(201, json=payload)
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        quotes = await c.numbers.quotes(country="pt", capabilities=["voice"])
        assert str(quotes.recommended_quote_id) == quote_id
        number = await c.numbers.acquire(
            country="PT", quote_id=quotes.recommended_quote_id
        )
        assert number.provisioning_state == "pending"
    assert json.loads(quotes_route.calls.last.request.content)["country_code"] == "PT"
    assert json.loads(purchase.calls.last.request.content)["quote_id"] == quote_id


@respx.mock
async def test_quotes_never_send_a_carrier(base_url: str, api_key: str):
    route = respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=httpx.Response(
            200,
            json={
                "offers": [],
                "recommended_quote_id": None,
                "incomplete": False,
                "expires_at": "2026-09-23T12:00:00Z",
            },
        )
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        await c.numbers.quotes(country="PT", capabilities=["voice"])
    assert "provider" not in json.loads(route.calls.last.request.content)


# --------------------------------------------------------------------------- #
# NumberOffer stays in sync with openapi.yaml
# --------------------------------------------------------------------------- #


def test_number_offer_matches_openapi_quote_offer() -> None:
    from pathlib import Path

    import pytest
    from hail import NumberOffer

    yaml = pytest.importorskip("yaml")
    spec = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "openapi" / "openapi.yaml").read_text()
    )
    schema = spec["components"]["schemas"]["QuoteOffer"]
    fields = NumberOffer.model_fields
    assert set(fields) == set(schema["properties"])
    assert {n for n, f in fields.items() if f.is_required()} == set(schema["required"])


def test_number_offer_accepts_a_minimal_and_a_full_offer() -> None:
    from hail import NumberOffer

    minimal = {
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
    # An older API still sends carrier fields: they parse and are dropped.
    old = NumberOffer.model_validate(
        {
            **minimal,
            "provider": "telnyx",
            "verification_id": "v1",
            "address_id": "a1",
            "quote_id": str(uuid4()),
        }
    )
    assert old.quote_id is not None
    assert not {"provider", "verification_id", "address_id"} & old.model_dump().keys()


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


# --------------------------------------------------------------------------- #
# numbers.route and agents
# --------------------------------------------------------------------------- #


@respx.mock
async def test_numbers_route_sends_only_given_fields(
    base_url: str, api_key: str
) -> None:
    number_id = uuid4()
    agent_id = uuid4()
    body = make_phone_number_response(number_id=number_id)
    body["voice_agent_id"] = str(agent_id)
    body["inbound_registered"] = True
    route = respx.patch(f"{base_url}/numbers/{number_id}").mock(
        return_value=httpx.Response(200, json=body)
    )
    async with Client(api_key=api_key, base_url=base_url) as client:
        n = await client.numbers.route(number_id, voice_agent_id=agent_id)
        assert n.voice_agent_id == agent_id
        assert n.inbound_registered is True
        assert json.loads(route.calls.last.request.content) == {
            "voice_agent_id": str(agent_id)
        }
        await client.numbers.route(number_id, sms_agent_id=None)
        assert json.loads(route.calls.last.request.content) == {"sms_agent_id": None}


def _agent_json(**over):
    base = {
        "id": str(uuid4()),
        "organization_id": str(uuid4()),
        "name": "Front desk",
        "system_prompt": "Book appointments.",
        "first_message": None,
        "ai_disclosure": True,
        "ai_disclosure_line": None,
        "voice_config": {},
        "tools": None,
        "max_duration_seconds": None,
        "sms_enabled": True,
        "status": "live",
        "created_at": "2026-10-02T12:00:00Z",
        "updated_at": "2026-10-02T12:00:00Z",
    }
    base.update(over)
    return base


@respx.mock
async def test_agents_create_list_update_delete(base_url: str, api_key: str) -> None:
    created = _agent_json(first_message="Hi.")
    agent_id = created["id"]
    respx.post(f"{base_url}/agents").mock(
        return_value=httpx.Response(201, json=created)
    )
    respx.get(f"{base_url}/agents").mock(
        return_value=httpx.Response(200, json={"items": [created]})
    )
    patch = respx.patch(f"{base_url}/agents/{agent_id}").mock(
        return_value=httpx.Response(200, json=_agent_json(id=agent_id, status="paused"))
    )
    delete = respx.delete(f"{base_url}/agents/{agent_id}").mock(
        return_value=httpx.Response(204)
    )
    async with Client(api_key=api_key, base_url=base_url) as client:
        a = await client.agents.create(
            name="Front desk",
            system_prompt="Book appointments.",
            first_message="Hi.",
            language="fr",
        )
        assert a.name == "Front desk"
        sent = json.loads(respx.calls[0].request.content)
        assert sent["voice_config"] == {"language": "fr"}
        assert "ai_disclosure_line" not in sent
        assert [x.id for x in (await client.agents.list()).items] == [UUID(agent_id)]
        updated = await client.agents.update(
            agent_id, status="paused", first_message=None
        )
        assert updated.status == "paused"
        assert json.loads(patch.calls.last.request.content) == {
            "status": "paused",
            "first_message": None,
        }
        await client.agents.delete(agent_id)
        assert delete.called


@respx.mock
async def test_calls_create_with_agent_id(base_url: str, api_key: str) -> None:
    from tests.conftest import make_call_response

    agent_id = uuid4()
    route = respx.post(f"{base_url}/calls").mock(
        return_value=httpx.Response(201, json=make_call_response())
    )
    async with Client(api_key=api_key, base_url=base_url) as client:
        await client.calls.create(
            to="+14155550100", recipient_consent=True, agent_id=agent_id
        )
    sent = json.loads(route.calls.last.request.content)
    assert sent["agent_id"] == str(agent_id)
    assert "system_prompt" not in sent


@respx.mock
async def test_quotes_expose_incomplete_and_verification(base_url: str, api_key: str):
    verification_id = uuid4()
    offer = {
        **_offer(uuid4(), readiness="verification_required"),
        "verification": {"id": str(verification_id), "state": "submitted"},
    }
    body = json.loads(_quotes_response([offer, _offer(uuid4())]).content)
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=httpx.Response(200, json={**body, "incomplete": True})
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        quotes = await c.numbers.quotes(country="US", capabilities=["voice"])
    assert quotes.incomplete is True
    assert quotes.offers[0].verification.id == verification_id
    assert quotes.offers[0].verification.state == "submitted"
    assert quotes.offers[1].verification is None


@respx.mock
async def test_quotes_default_when_the_api_omits_verification(
    base_url: str, api_key: str
):
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response([_offer(uuid4())])
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        quotes = await c.numbers.quotes(country="US", capabilities=["voice"])
    assert quotes.incomplete is False
    assert quotes.offers[0].verification is None


@respx.mock
async def test_numbers_acquire_sends_subject_type_only_when_given(
    base_url: str, api_key: str
) -> None:
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(
            201, json={**make_phone_number_response(), "subject_type": "business"}
        )
    )
    quote_id = uuid4()
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.acquire(
            country="GB", quote_id=quote_id, subject_type="business"
        )
        assert json.loads(route.calls.last.request.content)["subject_type"] == (
            "business"
        )
        assert number.subject_type == "business"
        await c.numbers.acquire(country="GB", quote_id=quote_id)
        assert "subject_type" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_quotes_expose_subject_types_and_verifications(
    base_url: str, api_key: str
):
    person = {"id": str(uuid4()), "state": "approved", "subject_type": "person"}
    offer = {
        **_offer(uuid4()),
        "subject_types": ["business", "person"],
        "verifications": [person],
        "verification": person,
    }
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response([offer])
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        quotes = await c.numbers.quotes(country="GB", capabilities=["voice"])
    (got,) = quotes.offers
    assert got.subject_types == ["business", "person"]
    assert got.verifications[0].subject_type == "person"


@respx.mock
async def test_numbers_acquire_sends_verification_id_and_reads_the_name(
    base_url: str, api_key: str
) -> None:
    vid = uuid4()
    route = respx.post(f"{base_url}/numbers").mock(
        return_value=httpx.Response(
            201,
            json={
                **make_phone_number_response(),
                "subject_type": "person",
                "verification_id": str(vid),
                "subject_name": "Ada Lovelace",
            },
        )
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        number = await c.numbers.acquire(
            country="GB", quote_id=uuid4(), verification_id=vid
        )
        assert json.loads(route.calls.last.request.content)["verification_id"] == (
            str(vid)
        )
        assert number.verification_id == vid
        assert number.subject_name == "Ada Lovelace"
        await c.numbers.acquire(country="GB", quote_id=uuid4())
        assert "verification_id" not in json.loads(route.calls.last.request.content)


@respx.mock
async def test_quotes_expose_verification_names(base_url: str, api_key: str):
    ada = {
        "id": str(uuid4()),
        "state": "approved",
        "subject_type": "person",
        "subject_name": "Ada Lovelace",
    }
    offer = {**_offer(uuid4()), "subject_types": ["person"], "verifications": [ada]}
    respx.post(f"{base_url}/numbers/quotes").mock(
        return_value=_quotes_response([offer])
    )
    async with Client(api_key=api_key, base_url=base_url) as c:
        quotes = await c.numbers.quotes(country="GB", capabilities=["voice"])
    assert quotes.offers[0].verifications[0].subject_name == "Ada Lovelace"
