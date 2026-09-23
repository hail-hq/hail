"""Read-only Telnyx inventory discovery; purchase integration is staged separately."""

from decimal import Decimal

import httpx
import phonenumbers
from hailhq.core.schemas import NumberType
from pydantic import BaseModel, Field


class NumberQuote(BaseModel):
    """Carrier costs, not customer prices or a guarantee of activation."""

    provider: str = "telnyx"
    e164: str
    country_code: str
    number_type: NumberType
    capabilities: list[str]
    upfront_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    monthly_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")


class TelnyxNumberDiscovery:
    """Uses an injected async client; the caller owns its lifetime.

    No ordering methods: inventory presence does not establish regulatory
    eligibility, outbound calling eligibility, or configured SIP routing.
    """

    def __init__(self, api_key: str, client: httpx.AsyncClient) -> None:
        if not api_key.strip():
            raise ValueError("Telnyx API key is required")
        self._api_key = api_key
        self._client = client

    async def search(
        self,
        country_code: str,
        number_type: NumberType,
        capabilities: list[str],
        limit: int = 20,
        *,
        outbound: bool = False,
        e164: str | None = None,
    ) -> list[NumberQuote]:
        country_code = country_code.upper()
        if (
            len(country_code) != 2
            or not country_code.isascii()
            or not country_code.isalpha()
        ):
            raise ValueError("Use a two-letter country code")
        if not 1 <= limit <= 100:
            raise ValueError("Limit must be between 1 and 100")
        requested = set(capabilities)
        if not requested or not requested <= {"voice", "sms", "mms", "fax"}:
            raise ValueError("Unsupported or empty capability selection")
        # Telnyx documents the emergency feature filter for outbound-capable
        # inventory. Voice alone establishes inbound voice, not origination.
        if outbound:
            requested.add("emergency")
        params = {
            "filter[country_code]": country_code,
            "filter[phone_number_type]": number_type,
            "filter[features]": ",".join(sorted(requested)),
            "filter[limit]": str(limit),
            "filter[best_effort]": "false",
        }
        if e164:
            params["filter[phone_number][ends_with]"] = (
                phonenumbers.national_significant_number(phonenumbers.parse(e164, None))
            )
        response = await self._client.get(
            "https://api.telnyx.com/v2/available_phone_numbers",
            headers={"Authorization": f"Bearer {self._api_key}"},
            params=params,
            timeout=20,
            follow_redirects=False,
        )
        response.raise_for_status()
        quotes = []
        for item in response.json()["data"]:
            if e164 and item["phone_number"] != e164:
                continue
            available = {feature["name"] for feature in item.get("features", [])}
            if item.get("best_effort") or not requested <= available:
                continue
            cost = item.get("cost_information")
            if not cost:
                continue  # Unknown costs must never become a zero-cost offer.
            quotes.append(
                NumberQuote(
                    e164=item["phone_number"],
                    country_code=country_code,
                    number_type=number_type,
                    capabilities=sorted(available),
                    upfront_cost=cost["upfront_cost"],
                    monthly_cost=cost["monthly_cost"],
                    currency=cost["currency"],
                )
            )
        return quotes
