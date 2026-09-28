from decimal import Decimal

import httpx
import pytest
from hailhq.core.providers.voice.telnyx import TelnyxNumberDiscovery
from pydantic import ValidationError


def offer(**changes):
    return {
        "phone_number": "+351300000001",
        "features": [{"name": "voice"}, {"name": "sms"}],
        "cost_information": {
            "upfront_cost": "2.15",
            "monthly_cost": "3.10",
            "currency": "USD",
        },
        **changes,
    }


@pytest.mark.asyncio
async def test_search_preserves_quotes_and_filters_capabilities():
    def handler(request):
        assert request.method == "GET"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.url.params["filter[country_code]"] == "PT"
        assert request.url.params["filter[phone_number_type]"] == "toll_free"
        assert request.url.params["filter[features]"] == "sms,voice"
        return httpx.Response(
            200,
            json={
                "data": [
                    offer(),
                    offer(features=[{"name": "voice"}]),
                    offer(best_effort=True),
                    offer(cost_information=None),
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        quotes = await TelnyxNumberDiscovery("test-key", client).search(
            "pt", "toll_free", ["voice", "sms"]
        )
    assert len(quotes) == 1
    assert quotes[0].monthly_cost == Decimal("3.10")
    assert quotes[0].upfront_cost == Decimal("2.15")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 429, 500, 302])
async def test_errors_are_not_empty_inventory(status):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                status, headers={"Location": "https://example.com"}
            )
        )
    ) as client:
        with pytest.raises(httpx.HTTPStatusError):
            await TelnyxNumberDiscovery("test-key", client).search(
                "PT", "local", ["voice"]
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-1"])
async def test_invalid_prices_fail_closed(amount):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "data": [
                        offer(
                            cost_information={
                                "upfront_cost": "0",
                                "monthly_cost": amount,
                                "currency": "USD",
                            }
                        )
                    ]
                },
            )
        )
    ) as client:
        with pytest.raises(ValidationError):
            await TelnyxNumberDiscovery("test-key", client).search(
                "PT", "local", ["voice"]
            )


async def test_exact_number_preflight_uses_national_digits_and_checks_e164():
    def handler(request):
        assert request.url.params["filter[phone_number][ends_with]"] == "300000001"
        # No "emergency" filter: that is a US/CA emergency-address feature, not
        # an outbound-calling requirement, and most non-US numbers lack it.
        assert request.url.params["filter[features]"] == "voice"
        return httpx.Response(
            200,
            json={
                "data": [
                    offer(features=[{"name": "voice"}, {"name": "emergency"}]),
                    offer(
                        phone_number="+351300000002",
                        features=[{"name": "voice"}, {"name": "emergency"}],
                    ),
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        quotes = await TelnyxNumberDiscovery("key", client).search(
            "PT", "local", ["voice"], e164="+351300000001"
        )
    assert [q.e164 for q in quotes] == ["+351300000001"]


@pytest.mark.parametrize("no_coverage", [True, False])
async def test_only_explicit_no_coverage_is_empty_inventory(no_coverage):
    detail = (
        "No coverage found in the specified country based on the provided search parameters."
        if no_coverage
        else "Invalid search filter"
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                400, json={"errors": [{"code": "10015", "detail": detail}]}
            )
        )
    ) as client:
        discovery = TelnyxNumberDiscovery("key", client)
        if no_coverage:
            assert await discovery.search("PT", "local", ["voice", "sms"]) == []
        else:
            with pytest.raises(httpx.HTTPStatusError):
                await discovery.search("PT", "local", ["voice", "sms"])


@pytest.mark.parametrize(
    "body",
    [
        {"errors": [{"code": "10015", "detail": None}]},
        {"errors": ["10015"]},
        {"errors": None},
        ["not", "an", "object"],
    ],
)
async def test_malformed_error_body_stays_a_carrier_error(body):
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(400, json=body))
    ) as client:
        discovery = TelnyxNumberDiscovery("key", client)
        with pytest.raises(httpx.HTTPStatusError):
            await discovery.search("PT", "local", ["voice", "sms"])
