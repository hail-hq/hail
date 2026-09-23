"""Live carrier inventory, prices and regulatory readiness. No country winners table."""

from __future__ import annotations

import asyncio
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.telnyx import TelnyxClient
from hailhq.core.providers.voice.telnyx import TelnyxNumberDiscovery
from hailhq.core.schemas import NumberType
from pydantic import BaseModel, Field
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client as TwilioClient


class CarrierOffer(BaseModel):
    provider: Literal["twilio", "telnyx"] = Field(
        description="Carrier supplying this exact number."
    )
    e164: str = Field(description="Available phone number in E.164 format.")
    country_code: str = Field(description="ISO alpha-2 country code of the number.")
    number_type: NumberType = Field(
        description="Local, mobile, national, or toll-free number type."
    )
    capabilities: list[str] = Field(
        description="Voice/SMS capabilities reported by live carrier inventory; SMS registration may still be required."
    )
    monthly_cents: int = Field(
        gt=0,
        description="Monthly number rental in USD cents, charged from organization credits.",
    )
    setup_cents: int = Field(
        ge=0,
        description="One-time setup charge in USD cents, payable with the first month.",
    )
    currency: Literal["USD"] = Field(
        default="USD", description="Currency of the quoted rental and setup amounts."
    )
    readiness: Literal["ready", "verification_required"] = Field(
        description="Whether regulatory preflight permits purchase for this organization; rechecked at purchase."
    )
    requirements: list[str] = Field(
        default_factory=list,
        description="Carrier regulatory requirement labels associated with this offer.",
    )
    verification_id: str | None = Field(
        default=None,
        description="Server-selected organization-bound approved bundle or requirement-group identifier, if any.",
    )
    address_id: str | None = Field(
        default=None,
        description="Server-selected verified address identifier, when supported; null otherwise.",
    )
    quote_id: UUID | None = Field(
        default=None,
        description="Organization-bound quote identifier to pass to POST /numbers before expiry.",
    )


def cents(value) -> int:
    price = Decimal(str(value))
    if not price.is_finite() or price < 0:
        raise ValueError("Invalid carrier price")
    return int((price * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def rank_offers(
    offers: list[CarrierOffer], provider: str = "auto"
) -> list[CarrierOffer]:
    """Ready first; cheapest monthly, then setup, then remaining requirements.

    Legally blocked stock is never recommended over activatable stock. Unknown
    prices/readiness are excluded by discovery, never interpreted as free/ready.
    """
    return sorted(
        (o for o in offers if provider == "auto" or o.provider == provider),
        key=lambda o: (
            o.readiness != "ready",
            o.monthly_cents,
            o.setup_cents,
            len(o.requirements),
            o.provider,
            o.e164,
        ),
    )


async def telnyx_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    client: httpx.AsyncClient,
    e164: str | None = None,
) -> list[CarrierOffer]:
    if not settings.telnyx_api_key:
        return []
    if "voice" in capabilities and not all(
        (
            settings.telnyx_connection_id,
            settings.livekit_telnyx_sip_outbound_trunk_id,
            settings.telnyx_sip_username,
        )
    ):
        return []
    if "sms" in capabilities and not settings.telnyx_public_key:
        return []
    api = TelnyxClient(client=client)
    quotes = await TelnyxNumberDiscovery(settings.telnyx_api_key, client).search(
        country,
        kind,
        capabilities,
        limit=1,
        outbound="voice" in capabilities,
        e164=e164,
    )
    if not quotes:
        return []
    groups = (
        await api.request(
            "GET",
            "/requirement_groups",
            params={
                "filter[country_code]": country,
                "filter[phone_number_type]": kind,
                "filter[action]": "ordering",
                "filter[status]": "approved",
                "filter[customer_reference]": f"hail-{org}",
            },
        )
    )["data"]
    group = next(
        (
            g
            for g in groups
            if g.get("customer_reference") == f"hail-{org}"
            and g.get("status") == "approved"
            and g.get("country_code") == country
            and g.get("phone_number_type") == kind
            and g.get("action") == "ordering"
        ),
        None,
    )
    results = []
    for q in quotes:
        if q.currency != "USD":
            continue
        params = {"filter[phone_number]": q.e164, "filter[action]": "ordering"}
        if group:
            params["filter[requirement_group_id]"] = group["id"]
        rules = (await api.request("GET", "/regulatory_requirements", params=params))[
            "data"
        ]
        # Missing country/type coverage is unknown, not an exemption.
        matched = [
            r
            for r in rules
            if r.get("country_code") == country
            and r.get("phone_number_type") == kind
            and r.get("action") == "ordering"
        ]
        if not matched:
            continue
        requirements = [r for rule in matched for r in rule["regulatory_requirements"]]
        labels = [
            r.get("name") or r.get("field_type") or "Verification" for r in requirements
        ]
        results.append(
            CarrierOffer(
                provider="telnyx",
                e164=q.e164,
                country_code=country,
                number_type=kind,
                capabilities=sorted(set(q.capabilities) & {"voice", "sms"}),
                monthly_cents=cents(q.monthly_cost),
                setup_cents=cents(q.upfront_cost),
                readiness=(
                    "ready" if not requirements or group else "verification_required"
                ),
                requirements=labels,
                verification_id=group["id"] if group else None,
            )
        )
    return results


async def twilio_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
) -> list[CarrierOffer]:
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        return []

    def discover():
        api = TwilioClient(
            settings.twilio_account_sid,
            settings.twilio_auth_token,
            http_client=TwilioHttpClient(timeout=10, max_retries=0),
        )
        inventory_api = getattr(api.available_phone_numbers(country), kind, None)
        if inventory_api is None:
            return []
        inventory = inventory_api.list(
            limit=3,
            **{f"{c}_enabled": True for c in capabilities},
            **({"contains": e164} if e164 else {}),
        )
        if e164:
            inventory = [n for n in inventory if n.phone_number == e164]
        if not inventory:
            return []
        pricing = api.pricing.v1.phone_numbers.countries(country).fetch()
        if (pricing.price_unit or "").upper() != "USD":
            return []
        prices = [
            p
            for p in (pricing.phone_number_prices or [])
            if p["number_type"].replace("-", "_").replace(" ", "_") == kind
        ]
        if not prices:
            return []
        compliance = api.numbers.v2.regulatory_compliance
        # Organizations are business end users. No hardcoded country exemptions.
        rules = compliance.regulations.list(
            iso_country=country,
            number_type=kind.replace("_", "-"),
            end_user_type="business",
            limit=100,
        )
        # Some countries return a regulation resource with no required fields
        # (US local is one). Presence of a resource alone is not a bundle gate.
        if any(not isinstance(r.requirements, dict) for r in rules):
            raise ValueError("Twilio regulatory requirements unavailable")
        rules = [r for r in rules if any(r.requirements.values())]
        bundle = None
        if rules:
            bundles = compliance.bundles.list(
                status="twilio-approved", friendly_name=f"hail-{org}", limit=100
            )
            bundle = next(
                (
                    b
                    for b in bundles
                    if b.friendly_name == f"hail-{org}"
                    and b.regulation_sid in {r.sid for r in rules}
                ),
                None,
            )
        result = []
        for n in inventory:
            address_required = n.address_requirements not in (None, "none")
            labels = ["Approved regulatory bundle"] if rules and not bundle else []
            if address_required:
                labels.append("Verified address for this number")
            result.append(
                CarrierOffer(
                    provider="twilio",
                    e164=n.phone_number,
                    country_code=country,
                    number_type=kind,
                    capabilities=sorted(
                        k.lower()
                        for k, v in n.capabilities.items()
                        if v and k.lower() in {"voice", "sms"}
                    ),
                    monthly_cents=cents(prices[0]["current_price"]),
                    setup_cents=0,
                    readiness="verification_required" if labels else "ready",
                    requirements=labels,
                    verification_id=bundle.sid if bundle else None,
                )
            )
        return result

    return await asyncio.to_thread(discover)


async def discover_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
) -> tuple[list[CarrierOffer], list[str]]:
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            twilio_offers(org, country, kind, capabilities, e164=e164),
            telnyx_offers(org, country, kind, capabilities, client, e164=e164),
            return_exceptions=True,
        )
    offers, unavailable = [], []
    for provider, result in zip(("twilio", "telnyx"), results):
        if isinstance(result, BaseException):
            unavailable.append(provider)
        else:
            offers.extend(result)
    return rank_offers(offers), unavailable
