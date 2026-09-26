"""Telnyx REST transport. Never retry mutations: carrier orders/sends cost money."""

from __future__ import annotations

import asyncio
import base64
import time
from urllib.parse import quote

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from hailhq.core.config import settings
from hailhq.core.urls import join_url

TELNYX_API_BASE = "https://api.telnyx.com/v2"

_http: httpx.AsyncClient | None = None
_http_loop: asyncio.AbstractEventLoop | None = None


def get_http_client() -> httpx.AsyncClient:
    """One connection pool for all Telnyx calls, built on first use.

    An httpx client is bound to the event loop that first used it, so a client
    made under a different (for example a closed test) loop is replaced.
    """
    global _http, _http_loop
    loop = asyncio.get_running_loop()
    if _http is None or _http_loop is not loop:
        _http, _http_loop = httpx.AsyncClient(), loop
    return _http


async def close_http_client() -> None:
    """Close the shared client. Call on application shutdown."""
    global _http, _http_loop
    client, _http, _http_loop = _http, None, None
    if client is not None:
        await client.aclose()


class TelnyxClient:
    def __init__(
        self, api_key: str | None = None, client: httpx.AsyncClient | None = None
    ):
        self.api_key = api_key if api_key is not None else settings.telnyx_api_key
        if not self.api_key:
            raise ValueError("Telnyx API key is not configured")
        self.client = client

    async def request(self, method: str, path: str, **kwargs) -> dict:
        response = await (self.client or get_http_client()).request(
            method,
            join_url(TELNYX_API_BASE, path),
            headers={"Authorization": f"Bearer {self.api_key}"},
            timeout=20,
            follow_redirects=False,
            **kwargs,
        )
        response.raise_for_status()
        return response.json() if response.content else {}

    async def release_number(self, resource_id: str) -> None:
        # Only a persisted owned-number id belongs here, never a number-order id.
        resource_id = path_id(resource_id)
        try:
            await self.request("DELETE", f"/phone_numbers/{resource_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise


def path_id(resource_id: str) -> str:
    """One URL path segment for a Telnyx resource id.

    Owned phone-number ids are numeric strings such as ``1293384261075731499``,
    not UUIDs; only number-order ids are UUIDs.
    """
    if not resource_id or not resource_id.strip():
        raise ValueError("Telnyx resource id is required")
    return quote(resource_id.strip(), safe="")


def verify_webhook(
    body: bytes,
    signature: str | None,
    timestamp: str | None,
    public_key: str,
    *,
    now: float | None = None,
) -> bool:
    """Verify the exact bytes, including a bounded signed timestamp."""
    if not signature or not timestamp or not public_key:
        return False
    try:
        if abs((time.time() if now is None else now) - int(timestamp)) > 300:
            return False
        key = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(public_key, validate=True)
        )
        key.verify(
            base64.b64decode(signature, validate=True), timestamp.encode() + b"|" + body
        )
        return True
    except (ValueError, OverflowError, InvalidSignature):
        # OverflowError: an absurdly long timestamp digit string cannot become a float.
        return False
