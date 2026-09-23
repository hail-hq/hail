"""Telnyx REST transport. Never retry mutations: carrier orders/sends cost money."""

from __future__ import annotations

import base64
import time
from uuid import UUID

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from hailhq.core.config import settings
from hailhq.core.urls import join_url

TELNYX_API_BASE = "https://api.telnyx.com/v2"


class TelnyxClient:
    def __init__(
        self, api_key: str | None = None, client: httpx.AsyncClient | None = None
    ):
        self.api_key = api_key if api_key is not None else settings.telnyx_api_key
        if not self.api_key:
            raise ValueError("Telnyx API key is not configured")
        self.client = client

    async def request(self, method: str, path: str, **kwargs) -> dict:
        async def send(client):
            response = await client.request(
                method,
                join_url(TELNYX_API_BASE, path),
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=20,
                follow_redirects=False,
                **kwargs,
            )
            response.raise_for_status()
            return response.json() if response.content else {}

        if self.client is not None:
            return await send(self.client)
        async with httpx.AsyncClient() as client:
            return await send(client)

    async def release_number(self, resource_id: str) -> None:
        # Only persisted carrier UUIDs belong here, never a number-order id.
        resource_id = str(UUID(resource_id))
        try:
            await self.request("DELETE", f"/phone_numbers/{resource_id}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise


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
    except (ValueError, InvalidSignature):
        return False
