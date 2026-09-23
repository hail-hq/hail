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
        assert request.url.params["filter[phone_number_type]"] == "toll-free"
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
