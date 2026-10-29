"""Telnyx number inventory discovery, order placement, order lookup and release."""

from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

import httpx
import phonenumbers
from hailhq.core.carrier_offer import Approval, CarrierOffer, cents
from hailhq.core.config import settings
from hailhq.core.providers.telnyx import (
    TelnyxClient,
    get_http_client,
    path_id,
    subject_types_for,
)
from hailhq.core.providers.voice.base import CarrierNotConfigured, OrderState
from hailhq.core.schemas import NumberType
from pydantic import BaseModel, Field


class NumberQuote(BaseModel):
    """Carrier costs, not customer prices or a guarantee of activation."""

    e164: str
    country_code: str
    number_type: NumberType
    capabilities: list[str]
    upfront_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    monthly_cost: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    locality: str | None = None
    region: str | None = None


# Telnyx error code -> detail prefix that means "no stock", not an outage.
# 10031 comes back when a filter (such as an area code) matches no numbers.
_EMPTY_INVENTORY = {
    "10015": "No coverage found in the specified country",
    "10031": "No numbers found for the given filters",
}


def _no_stock(error: object) -> bool:
    if not isinstance(error, dict):
        return False
    prefix = _EMPTY_INVENTORY.get(str(error.get("code")))
    return prefix is not None and str(error.get("detail") or "").startswith(prefix)


class TelnyxNumberDiscovery:
    """Uses an injected async client; the caller owns its lifetime.

    Read-only: inventory presence does not establish regulatory eligibility,
    outbound calling eligibility, or configured SIP routing. Ordering lives in
    ``place_number_order``.
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
        e164: str | None = None,
        area_code: str | None = None,
        city: str | None = None,
        region: str | None = None,
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
        if area_code:
            # The digits after the country code: works in every country,
            # unlike national_destination_code.
            params["filter[phone_number][starts_with]"] = area_code
        if city:
            params["filter[locality]"] = city
        if region:
            params["filter[administrative_area]"] = region
        try:
            data = await TelnyxClient(self._api_key, self._client).request(
                "GET", "/available_phone_numbers", params=params
            )
        except httpx.HTTPStatusError as exc:
            # HTTPStatusError retains the raw body. Only explicit no-coverage
            # responses become empty inventory; other errors stay errors.
            try:
                body = exc.response.json()
            except ValueError:
                body = None
            errors = body.get("errors") if isinstance(body, dict) else None
            if (
                exc.response.status_code == 400
                and isinstance(errors, list)
                and errors
                and all(_no_stock(error) for error in errors)
            ):
                return []
            raise
        quotes = []
        for item in data["data"]:
            if e164 and item["phone_number"] != e164:
                continue
            available = {feature["name"] for feature in item.get("features", [])}
            if item.get("best_effort") or not requested <= available:
                continue
            cost = item.get("cost_information")
            if not cost:
                continue  # Unknown costs must never become a zero-cost offer.
            where = {
                r.get("region_type"): r.get("region_name")
                for r in item.get("region_information") or []
                if isinstance(r, dict)
            }
            quotes.append(
                NumberQuote(
                    e164=item["phone_number"],
                    country_code=country_code,
                    number_type=number_type,
                    capabilities=sorted(available),
                    upfront_cost=cost["upfront_cost"],
                    monthly_cost=cost["monthly_cost"],
                    currency=cost["currency"],
                    # Telnyx names places in capitals, some with the
                    # country: "PHOENIX", "LONDON, GB".
                    locality=(where.get("location") or "").split(",")[0].title()
                    or None,
                    region=where.get("state"),
                )
            )
        return quotes


async def telnyx_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    client: httpx.AsyncClient,
    e164: str | None = None,
    area_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    approvals: list[Approval] | None = None,
) -> list[CarrierOffer]:
    """Live Telnyx inventory. ``approvals`` are the org's approved Hail
    verifications; one counts while its requirement group is still approved
    at Telnyx for this country and type. Telnyx does not tie a group to a
    subject type, so either kind is accepted, except where only a business
    can meet the rules."""
    if not settings.telnyx_api_key:
        return []
    approvals = approvals or []
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
    # Store only what was requested and what this deployment can route: voice
    # needs the SIP trunk (checked above), sms needs the webhook key.
    routable = {"voice", "sms"}
    quotes = await TelnyxNumberDiscovery(settings.telnyx_api_key, client).search(
        country,
        kind,
        capabilities,
        limit=3,
        e164=e164,
        area_code=area_code,
        city=city,
        region=region,
    )
    if not quotes:
        return []
    valid: list[Approval] = []
    if approvals:
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
        approved = {
            g.get("id")
            for g in groups
            if g.get("customer_reference") == f"hail-{org}"
            and g.get("status") == "approved"
            and g.get("country_code") == country
            and g.get("phone_number_type") == kind
            and g.get("action") == "ordering"
        }
        valid = [
            a for a in approvals if a.verification_id and a.verification_id in approved
        ]
    # Any approved group answers which rules are left; they are kind-blind.
    group = next(iter(valid), None)
    results = []
    for q in quotes:
        monthly_cents = cents(q.monthly_cost)
        # Only USD offers with a positive monthly price count; a zero or
        # sub-cent price must skip this offer, not fail the whole carrier.
        if q.currency != "USD" or monthly_cents <= 0:
            continue
        params = {"filter[phone_number]": q.e164, "filter[action]": "ordering"}
        if group:
            params["filter[requirement_group_id]"] = group.verification_id
        rules = (await api.request("GET", "/regulatory_requirements", params=params))[
            "data"
        ]
        # An empty answer is Telnyx saying this number has no ordering rules
        # (US and CA local, for example): the offer is ready. A non-empty
        # answer that names another country or type is unknown coverage, not
        # an exemption.
        matched = [
            r
            for r in rules
            if r.get("country_code") == country
            and r.get("phone_number_type") == kind
            and r.get("action") == "ordering"
        ]
        if rules and not matched:
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
                capabilities=sorted(set(q.capabilities) & set(capabilities) & routable),
                monthly_cents=monthly_cents,
                setup_cents=cents(q.upfront_cost),
                readiness=(
                    "ready" if not requirements or group else "verification_required"
                ),
                regulatory_friction=(
                    "none"
                    if not requirements or group
                    else (
                        "documents"
                        if any(r.get("field_type") == "document" for r in requirements)
                        else (
                            "information"
                            if all(
                                r.get("field_type") in {"textual", "address"}
                                for r in requirements
                            )
                            else "unknown"
                        )
                    )
                ),
                requirements=labels,
                # With a group passed, Telnyx may list no rules left; the
                # number still needs that group at purchase.
                subject_types=(
                    sorted(subject_types_for(requirements))
                    if requirements or group
                    else []
                ),
                approvals=valid if requirements or group else [],
                locality=q.locality,
                region=q.region,
            )
        )
        break  # One offer per carrier; a skipped result falls to the next.
    return results


async def place_number_order(
    number_id: UUID, e164: str, verification_id: str | None, capabilities: list[str]
) -> str:
    """Order one exact number once; returns the Telnyx order id. Never retried."""
    order_number: dict[str, Any] = {"phone_number": e164}
    if verification_id:
        order_number["requirement_group_id"] = verification_id
    payload: dict[str, Any] = {
        "phone_numbers": [order_number],
        "customer_reference": str(number_id),
    }
    if "voice" in capabilities and settings.telnyx_connection_id:
        payload["connection_id"] = settings.telnyx_connection_id
    order = (await TelnyxClient().request("POST", "/number_orders", json=payload))[
        "data"
    ]
    return order["id"]


async def telnyx_order_outcome(
    e164: str, number_id: UUID, order_id: str | None
) -> tuple[Literal["active", "failed", "pending", "missing"], str | None, str | None]:
    """Ask Telnyx what happened to an order. Holds no DB lock.

    Returns (state, owned resource id, Telnyx order id). ``missing`` means
    Telnyx has no record of the order; it is never treated as permission to
    submit another paid purchase.
    """
    api = TelnyxClient()
    if order_id:
        order = (await api.request("GET", f"/number_orders/{UUID(order_id)}"))["data"]
    else:
        # A crash/timeout after POST may lose the response. Recover by our
        # durable reference.
        result = await api.request(
            "GET",
            "/number_orders",
            params={
                "filter[customer_reference]": str(number_id),
                "page[size]": 100,
            },
        )
        matches = [
            o for o in result["data"] if o.get("customer_reference") == str(number_id)
        ]
        if not matches:
            return "missing", None, None
        if len(matches) > 1:
            return "pending", None, None
        order = matches[0]
    order_id = order.get("id") or order_id
    if order["status"] == "failure":
        return "failed", None, order_id
    # Only an explicit False keeps the order pending. A missing or null flag
    # is decided by the owned-number check below.
    if order["status"] != "success" or order.get("requirements_met") is False:
        return "pending", None, order_id
    owned = (
        await api.request(
            "GET",
            "/phone_numbers",
            params={"filter[phone_number]": e164.lstrip("+"), "page[size]": 100},
        )
    )["data"]
    match = next(
        (n for n in owned if n["phone_number"] == e164 and n.get("status") == "active"),
        None,
    )
    # Order-phone IDs and owned-phone IDs are distinct Telnyx resources.
    return (
        ("active", match["id"], order["id"])
        if match
        else ("pending", None, order["id"])
    )


async def release_telnyx_number(resource_id: str) -> None:
    """Release an owned Telnyx number. Raises ``CarrierNotConfigured`` if the
    API key is missing."""
    try:
        client = TelnyxClient()
    except ValueError as exc:
        raise CarrierNotConfigured(str(exc)) from exc
    await client.release_number(resource_id)


# -- carrier interface (core/hailhq/core/carrier_routing.py) ---------------


async def offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
    area_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    approvals: list[Approval] | None = None,
) -> list[CarrierOffer]:
    return await telnyx_offers(
        org,
        country,
        kind,
        capabilities,
        get_http_client(),
        e164=e164,
        area_code=area_code,
        city=city,
        region=region,
        approvals=approvals,
    )


async def place_order(number_id: UUID, offer: CarrierOffer) -> str:
    return await place_number_order(
        number_id, offer.e164, offer.verification_id, offer.capabilities
    )


async def order_outcome(
    e164: str, number_id: UUID, order_id: str | None, offer: CarrierOffer
) -> tuple[OrderState, str | None, str | None]:
    return await telnyx_order_outcome(e164, number_id, order_id)


async def release(resource_id: str) -> None:
    await release_telnyx_number(resource_id)


# --- inbound: a Telnyx number takes calls through the FQDN connection whose
# FQDN is LiveKit's SIP endpoint (docs/public/self-host/telnyx.md). Orders
# already set connection_id; this covers numbers bought before that and the
# detach on release.


def _connection_client() -> TelnyxClient:
    try:
        client = TelnyxClient()
    except ValueError as exc:
        raise CarrierNotConfigured(str(exc)) from exc
    if not settings.telnyx_connection_id:
        raise CarrierNotConfigured("TELNYX_CONNECTION_ID is not set")
    return client


async def _set_connection(resource_id: str | None, e164: str, connection: str) -> None:
    if not resource_id:
        raise CarrierNotConfigured(f"{e164} has no Telnyx phone number id")
    client = _connection_client()
    await client.request(
        "PATCH",
        f"/phone_numbers/{path_id(resource_id)}/voice",
        json={"connection_id": connection},
    )


async def attach_inbound_number(resource_id: str | None, e164: str) -> None:
    await _set_connection(resource_id, e164, settings.telnyx_connection_id)


async def detach_inbound_number(resource_id: str | None, e164: str) -> None:
    # The same connection carries outbound calls (set at purchase), so it stays.
    # The LiveKit inbound trunk number list is what gates inbound calls.
    return None
