"""DIDWW inbound routing through API v3 (JSON:API).

A DID takes calls through the voice IN trunk it is assigned to. Hail keeps
one SIP trunk pointed at LiveKit (``DIDWW_VOICE_IN_TRUNK_ID``,
docs/public/self-host/didww.md) and assigns a DID to it when the number
routes calls to an agent.
"""

from __future__ import annotations

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.voice.base import CarrierNotConfigured, CarrierRequestError
from hailhq.core.urls import join_url

_BASES = {
    "production": "https://api.didww.com/v3",
    "sandbox": "https://sandbox-api.didww.com/v3",
}
_JSON_API = "application/vnd.api+json"


def _base_url() -> str:
    return _BASES.get(settings.didww_environment, _BASES["production"])


def _headers() -> dict[str, str]:
    if not settings.didww_api_key:
        raise CarrierNotConfigured("DIDWW_API_KEY is not set")
    return {"Api-Key": settings.didww_api_key, "Content-Type": _JSON_API}


async def _request(method: str, path: str, **kwargs) -> dict:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.request(
            method, join_url(_base_url(), path), headers=_headers(), **kwargs
        )
    if response.status_code >= 400:
        raise CarrierRequestError(response.status_code)
    return response.json() if response.content else {}


async def _did_id(resource_id: str | None, e164: str) -> str:
    """The DID's id: the stored resource id, else a lookup by number (rows
    registered by hand before Hail stored DID ids)."""
    if resource_id:
        return resource_id
    found = await _request("GET", "/dids", params={"filter[number]": e164.lstrip("+")})
    for did in found.get("data", []):
        if did.get("attributes", {}).get("number") == e164.lstrip("+"):
            return did["id"]
    raise CarrierNotConfigured(f"{e164} is not a DID on this DIDWW account")


async def _set_voice_in_trunk(did_id: str, trunk_id: str | None) -> None:
    data = {"type": "voice_in_trunks", "id": trunk_id} if trunk_id else None
    await _request(
        "PATCH",
        f"/dids/{did_id}",
        json={
            "data": {
                "id": did_id,
                "type": "dids",
                "relationships": {"voice_in_trunk": {"data": data}},
            }
        },
    )


async def attach_inbound_number(resource_id: str | None, e164: str) -> None:
    if not settings.didww_voice_in_trunk_id:
        raise CarrierNotConfigured("DIDWW_VOICE_IN_TRUNK_ID is not set")
    did_id = await _did_id(resource_id, e164)
    await _set_voice_in_trunk(did_id, settings.didww_voice_in_trunk_id)


async def detach_inbound_number(resource_id: str | None, e164: str) -> None:
    try:
        did_id = await _did_id(resource_id, e164)
    except CarrierNotConfigured:
        return  # already gone from the account
    try:
        await _set_voice_in_trunk(did_id, None)
    except CarrierRequestError as exc:
        if exc.status != 404:
            raise
