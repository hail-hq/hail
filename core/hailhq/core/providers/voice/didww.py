"""DIDWW carrier: offers, orders, registration outcome, release.

All HTTP goes through the ``didww`` SDK's low-level client with plain
JSON:API dicts, so tests mock at the ``requests`` boundary (``responses``)
and SDK drift shows up as test failures. The SDK is sync; every public
function here is ``async`` and runs its call in ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache
from typing import Literal
from uuid import UUID

import requests
from didww.client import DidwwClient
from didww.configuration import Environment
from didww.exceptions import DidwwApiError
from hailhq.core.carrier_offer import CarrierOffer, cents
from hailhq.core.config import settings
from hailhq.core.providers.voice.base import (
    CarrierNotConfigured,
    CarrierPreOrderError,
    CarrierRequestError,
)
from hailhq.core.schemas import NumberType

logger = logging.getLogger(__name__)

PROVIDER = "didww"

_ENVIRONMENTS = {
    "production": Environment.PRODUCTION,
    "sandbox": Environment.SANDBOX,
}


# Seconds before a DIDWW call gives up. The SDK sets no timeout of its own.
DIDWW_TIMEOUT_SECONDS = 20


class _TimeoutAdapter(requests.adapters.HTTPAdapter):
    """Adds ``DIDWW_TIMEOUT_SECONDS`` to every request that has none.
    ``requests`` passes ``timeout=None`` explicitly, so ``setdefault`` alone
    would never apply it."""

    def send(self, request, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = DIDWW_TIMEOUT_SECONDS
        return super().send(request, **kwargs)


@lru_cache(maxsize=2)
def _client_for(api_key: str, env: Environment) -> DidwwClient:
    client = DidwwClient(api_key=api_key, environment=env)
    # The SDK copies any Session passed in, so mount on its own session.
    client._session.mount("https://", _TimeoutAdapter())
    return client


def didww_client() -> DidwwClient:
    """The client for the configured environment, one per process so every
    call shares its connections. ``CarrierNotConfigured`` when the key is
    missing or the environment name is unknown."""
    if not settings.didww_api_key:
        raise CarrierNotConfigured("DIDWW is not configured (DIDWW_API_KEY)")
    env = _ENVIRONMENTS.get(settings.didww_environment)
    if env is None:
        raise CarrierNotConfigured(
            "DIDWW_ENVIRONMENT must be 'production' or 'sandbox'"
        )
    return _client_for(settings.didww_api_key, env)


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
    address whose papers passed DIDWW's validation. No person reviews them."""
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
    an ``sms`` request never gets a DIDWW offer. No offer without the
    outbound trunk either: a number that cannot dial is never sold."""
    if (
        not settings.didww_api_key
        or not settings.livekit_didww_sip_outbound_trunk_id
        or "sms" in capabilities
    ):
        return []
    return await asyncio.to_thread(_offers_sync, org, country, kind, e164)


OrderState = Literal["active", "failed", "pending", "missing", "rejected_registration"]


def _find_available(client: DidwwClient, e164: str) -> tuple[str, str]:
    """(available_did id, metered sku id) for one exact number. Raises
    ``CarrierPreOrderError``: 410 when the number is gone, the carrier's
    status when the search itself failed. This inventory search runs
    before any order is placed, so the outcome is never ambiguous: no
    order exists yet and the caller can fail and refund immediately."""
    try:
        body = client.get(
            "available_dids",
            params={
                "filter[number_contains]": e164.lstrip("+"),
                "include": "did_group,did_group.stock_keeping_units",
                "page[size]": 10,
            },
        )
    except DidwwApiError as exc:
        raise CarrierPreOrderError(carrier_status(exc)) from exc
    except requests.RequestException as exc:
        raise CarrierPreOrderError(502) from exc
    index = _by_id(body.get("included", []))
    for did in body["data"]:
        if "+" + did["attributes"]["number"] != e164:
            continue
        group = index.get(
            ("did_groups", did["relationships"]["did_group"]["data"]["id"])
        )
        sku = _metered_sku(group, index) if group else None
        if sku is not None:
            return did["id"], sku["id"]
    raise CarrierPreOrderError(410)


def _place_order_sync(number_id: UUID, e164: str) -> str:
    client = didww_client()
    available_id, sku_id = _find_available(client, e164)
    try:
        order = client.post(
            "orders",
            {
                "data": {
                    "type": "orders",
                    "attributes": {
                        "allow_back_ordering": False,
                        "external_reference_id": str(number_id),
                        "items": [
                            {
                                "type": "did_order_items",
                                "attributes": {
                                    "available_did_id": available_id,
                                    "sku_id": sku_id,
                                },
                            }
                        ],
                    },
                }
            },
        )
    except DidwwApiError as exc:
        raise CarrierRequestError(carrier_status(exc)) from exc
    return order["data"]["id"]


async def place_didww_order(number_id: UUID, e164: str, address_id: str | None) -> str:
    """Order one exact number once; returns the DIDWW order id. Never
    retried. ``address_id`` is not sent: DIDWW links the registration to
    the DID after the order (see ``didww_order_outcome``)."""
    return await asyncio.to_thread(_place_order_sync, number_id, e164)


def _load_orders(
    client: DidwwClient, number_id: UUID, order_id: str | None
) -> list[dict]:
    """The stored order, or every order that carries our reference."""
    if order_id:
        return [client.get(f"orders/{order_id}")["data"]]
    # A crash after POST may lose the response. Recover by our reference.
    found = client.get(
        "orders",
        params={"filter[external_reference_id]": str(number_id), "page[size]": 10},
    )["data"]
    return [
        o
        for o in found
        if o["attributes"].get("external_reference_id") == str(number_id)
    ]


def _order_dids(client: DidwwClient, order_id: str) -> list[dict]:
    return client.get(
        "dids",
        params={
            "filter[order.id]": order_id,
            "include": "address_verification",
            "page[size]": 10,
        },
    )["data"]


def _holds(dids: list[dict], e164: str) -> dict | None:
    return next((d for d in dids if "+" + d["attributes"]["number"] == e164), None)


def _owner(client: DidwwClient, e164: str, orders: list[dict]) -> dict | None:
    """Of several orders with our reference, the one that bought ``e164``.
    None while no completed order holds the number."""
    for order in orders:
        if order["attributes"]["status"] == "completed" and _holds(
            _order_dids(client, order["id"]), e164
        ):
            return order
    return None


def _ensure_verification(client: DidwwClient, did: dict, address_id: str) -> dict:
    """The DID's address verification, created once when missing."""
    rel = did["relationships"].get("address_verification", {}).get("data")
    if rel:
        return client.get(f"address_verifications/{rel['id']}")["data"]
    address = client.get(f"addresses/{address_id}")["data"]
    return client.post(
        "address_verifications",
        {
            "data": {
                "type": "address_verifications",
                "attributes": {
                    "service_description": address["attributes"].get("description")
                    or ""
                },
                "relationships": {
                    "address": {"data": {"id": address_id, "type": "addresses"}},
                    "dids": {"data": [{"id": did["id"], "type": "dids"}]},
                },
            }
        },
    )["data"]


def _outcome_sync(
    e164: str, number_id: UUID, order_id: str | None, address_id: str | None
) -> tuple[OrderState, str | None, str | None]:
    client = didww_client()
    orders = _load_orders(client, number_id, order_id)
    if not orders:
        return "missing", None, None
    if len(orders) > 1:
        # The order id was lost and several orders carry our reference.
        if all(o["attributes"]["status"] == "canceled" for o in orders):
            return "failed", None, None
        owner = _owner(client, e164, orders)
        if owner is None:
            # DIDWW knows the order; which one is ours is not known yet.
            # Never 'missing' (same as Telnyx).
            return "pending", None, None
        orders = [owner]
    order = orders[0]
    order_id = order["id"]
    status = order["attributes"]["status"]
    if status == "canceled":
        return "failed", None, order_id
    if status != "completed":
        return "pending", None, order_id
    did = _holds(_order_dids(client, order_id), e164)
    if did is None:
        return "pending", None, order_id
    if not did["attributes"].get("awaiting_registration"):
        return "active", did["id"], order_id
    # From here the DID exists: a pending result carries its id so a timeout
    # can stop its renewal. The id is stored only on active/rejected.
    if not address_id:
        # Bought without an approved registration: nothing to file.
        return "pending", did["id"], order_id
    verification = _ensure_verification(client, did, address_id)
    vstatus = verification["attributes"]["status"]
    if vstatus == "approved":
        return "active", did["id"], order_id
    if vstatus == "rejected":
        return "rejected_registration", did["id"], order_id
    return "pending", did["id"], order_id


async def didww_order_outcome(
    e164: str, number_id: UUID, order_id: str | None, address_id: str | None
) -> tuple[OrderState, str | None, str | None]:
    """Ask DIDWW what happened to an order. Holds no DB lock. Files the
    end-user registration once the DID exists. Errors propagate so the
    reconciler retries until the carrier's ``pending_timeout``."""
    return await asyncio.to_thread(_outcome_sync, e164, number_id, order_id, address_id)


def _terminate_sync(did_id: str) -> None:
    didww_client().patch(
        f"dids/{did_id}",
        {"data": {"id": did_id, "type": "dids", "attributes": {"terminated": True}}},
    )


async def terminate_did(did_id: str) -> None:
    """Stop renewal at the end of the billing cycle. DIDWW does not refund."""
    await asyncio.to_thread(_terminate_sync, did_id)


def rejected_address_ref(org: UUID, country: str, kind: str) -> str:
    """``external_reference_id`` of an address whose registration DIDWW
    rejected. Discovery only matches ``hail:``, so the org must register
    again before it can buy."""
    return f"hail-rejected:{org}:{country}:{kind}"


def _revoke_sync(address_id: str, org: UUID, country: str, kind: str) -> str | None:
    client = didww_client()
    client.patch(
        f"addresses/{address_id}",
        {
            "data": {
                "id": address_id,
                "type": "addresses",
                "attributes": {
                    "external_reference_id": rejected_address_ref(org, country, kind)
                },
            }
        },
    )
    found = client.get(
        "address_verifications", params={"filter[address.id]": address_id}
    )["data"]
    rejected = [v for v in found if v["attributes"].get("status") == "rejected"]
    if not rejected:
        return None
    newest = max(rejected, key=lambda v: v["attributes"].get("created_at") or "")
    attrs = newest["attributes"]
    parts = [
        "; ".join(r for r in attrs.get("reject_reasons") or [] if r),
        attrs.get("reject_comment") or "",
    ]
    return " - ".join(p.strip() for p in parts if p.strip()) or None


async def revoke_registration(
    address_id: str, org: UUID, country: str, kind: str
) -> str | None:
    """Take back an approved registration DIDWW rejected, so the next quote
    asks for new papers. Returns DIDWW's rejection reason, or None. Errors
    propagate."""
    return await asyncio.to_thread(_revoke_sync, address_id, org, country, kind)


async def release_didww_number(resource_id: str) -> None:
    """Release an owned DIDWW number. ``CarrierNotConfigured`` when the key
    is missing; a 404 means it is already gone and is tolerated."""
    try:
        await terminate_did(resource_id)
    except DidwwApiError as exc:
        if carrier_status(exc) == 404:
            logger.warning(
                "didww terminate of %s returned 404; treating as already released",
                resource_id,
            )
            return
        raise
