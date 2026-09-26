"""DIDWW carrier: offers, orders, registration outcome, release.

All HTTP goes through the ``didww`` SDK's low-level client with plain
JSON:API dicts, so tests mock at the ``requests`` boundary (``responses``)
and SDK drift shows up as test failures. The SDK is sync; every public
function here is ``async`` and runs its call in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Literal
from uuid import UUID

from didww.client import DidwwClient
from didww.configuration import Environment
from didww.exceptions import DidwwApiError
from hailhq.core.carrier_offer import CarrierOffer, cents
from hailhq.core.config import settings
from hailhq.core.providers.voice.base import CarrierNotConfigured
from hailhq.core.schemas import NumberType

PROVIDER = "didww"

_ENVIRONMENTS = {
    "production": Environment.PRODUCTION,
    "sandbox": Environment.SANDBOX,
}


def didww_client() -> DidwwClient:
    """A client for the configured environment. ``CarrierNotConfigured``
    when the key is missing or the environment name is unknown."""
    if not settings.didww_api_key:
        raise CarrierNotConfigured("DIDWW is not configured (DIDWW_API_KEY)")
    env = _ENVIRONMENTS.get(settings.didww_environment)
    if env is None:
        raise CarrierNotConfigured(
            "DIDWW_ENVIRONMENT must be 'production' or 'sandbox'"
        )
    return DidwwClient(api_key=settings.didww_api_key, environment=env)


def carrier_status(exc: DidwwApiError) -> int:
    """HTTP status of a DIDWW error; 502 when the SDK did not record one."""
    return exc.status_code or 502


_TYPE_NAMES = {
    "Local": "local",
    "National": "national",
    "Mobile": "mobile",
    "Toll-free": "toll_free",
}


def approved_address_ref(org: UUID, country: str, kind: str) -> str:
    """``external_reference_id`` the verification plug-in stamps on an
    address whose papers passed validation and a superadmin approved."""
    return f"hail:{org}:{country}:{kind}"


@lru_cache(maxsize=64)
def lookup_ids(country: str) -> tuple[str | None, dict[str, str]]:
    """(country id, {hail number type: DIDWW group type id}). Cached per
    process; these ids never change."""
    client = didww_client()
    countries = client.get("countries", params={"filter[iso]": country})["data"]
    country_id = countries[0]["id"] if countries else None
    if not country_id:
        return None, {}
    types = {
        _TYPE_NAMES[t["attributes"]["name"]]: t["id"]
        for t in client.get("did_group_types")["data"]
        if t["attributes"]["name"] in _TYPE_NAMES
    }
    return country_id, types


def _by_id(included: list[dict]) -> dict[tuple[str, str], dict]:
    return {(r["type"], r["id"]): r for r in included}


def _metered_sku(group: dict, index: dict) -> dict | None:
    for ref in group["relationships"].get("stock_keeping_units", {}).get("data", []):
        sku = index.get(("stock_keeping_units", ref["id"]))
        if sku and sku["attributes"].get("channels_included_count") == 0:
            return sku
    return None


def _friction(requirement: dict | None) -> Literal["information", "documents"]:
    attrs = (requirement or {}).get("attributes", {})
    qty = (
        attrs.get("personal_proof_qty", 0)
        + attrs.get("business_proof_qty", 0)
        + attrs.get("address_proof_qty", 0)
    )
    return "documents" if qty > 0 else "information"


def _offers_sync(
    org: UUID, country: str, kind: NumberType, e164: str | None
) -> list[CarrierOffer]:
    client = didww_client()
    country_id, types = lookup_ids(country)
    type_id = types.get(kind)
    if not country_id or not type_id:
        return []
    params = {
        "filter[country.id]": country_id,
        "filter[did_group_type.id]": type_id,
        "filter[did_group.features]": "voice_out",
        "include": "did_group,did_group.stock_keeping_units,did_group.address_requirement",
        "page[size]": 3,
    }
    if e164:
        params["filter[number_contains]"] = e164.lstrip("+")
    body = client.get("available_dids", params=params)
    index = _by_id(body.get("included", []))
    approved: dict | None = None
    approved_checked = False
    results: list[CarrierOffer] = []
    for did in body["data"]:
        number = "+" + did["attributes"]["number"]
        if e164 and number != e164:
            continue
        group = index.get(
            ("did_groups", did["relationships"]["did_group"]["data"]["id"])
        )
        if not group or "voice_out" not in group["attributes"].get("features", []):
            continue
        sku = _metered_sku(group, index)
        if sku is None:
            continue
        monthly = cents(sku["attributes"]["monthly_price"])
        if monthly <= 0:
            continue
        needs_registration = bool(group.get("meta", {}).get("needs_registration"))
        if needs_registration and not approved_checked:
            approved_checked = True
            found = client.get(
                "addresses",
                params={
                    "filter[external_reference_id]": approved_address_ref(
                        org, country, kind
                    )
                },
            )["data"]
            approved = found[0] if found else None
        req_ref = group["relationships"].get("address_requirement", {}).get("data")
        requirement = (
            index.get(("address_requirements", req_ref["id"])) if req_ref else None
        )
        ready = not needs_registration or approved is not None
        results.append(
            CarrierOffer(
                provider=PROVIDER,
                e164=number,
                country_code=country,
                number_type=kind,
                capabilities=["voice"],
                monthly_cents=monthly,
                setup_cents=cents(sku["attributes"].get("setup_price") or 0),
                readiness="ready" if ready else "verification_required",
                regulatory_friction="none" if ready else _friction(requirement),
                requirements=(
                    [] if ready else ["End-user registration (identity and address)"]
                ),
                verification_id=approved["id"] if approved else None,
                address_id=approved["id"] if approved else None,
            )
        )
        break  # One offer per carrier, like Telnyx.
    return results


async def didww_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
) -> list[CarrierOffer]:
    """Live DIDWW inventory for one country and number type. Voice only:
    an ``sms`` request never gets a DIDWW offer."""
    if not settings.didww_api_key or "sms" in capabilities:
        return []
    return await asyncio.to_thread(_offers_sync, org, country, kind, e164)
