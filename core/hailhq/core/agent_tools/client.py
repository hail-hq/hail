"""HMAC-signed HTTP client for voicebot → API internal agent routes.

Same signing scheme as everywhere else in this repo
(``hailhq.core.hmac_signing``): HMAC-SHA256 over the raw request body in
``X-Hail-Signature``. One retry on timeout OR connection drop (e.g. a
mid-response ``ServerDisconnectedError``) is safe because the retry resends
the exact same signed body/``tool_invocation_id`` and the routes dedupe on
that id — so the resend can never become a duplicate send.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import aiohttp

from hailhq.core import hmac_signing
from hailhq.core.urls import join_url

logger = logging.getLogger("hailhq.core.agent_tools")

_TIMEOUT_SECONDS = 10.0


class AgentApiClient:
    def __init__(self, base_url: str, secret: str) -> None:
        self._base_url = base_url
        self._secret = secret
        self._session: aiohttp.ClientSession | None = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=_TIMEOUT_SECONDS)
            )
        return self._session

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        """POST a signed payload, retrying once on timeout or connection drop.

        ``asyncio.TimeoutError`` covers request timeouts (and
        ``aiohttp.ServerTimeoutError``, which subclasses it).
        ``aiohttp.ClientConnectionError`` covers connection-level failures
        such as ``ServerDisconnectedError`` or ``ClientConnectorError`` —
        cases where the request may never have reached the server, or the
        response was lost in flight. Both are safe to retry with the same
        body because the route dedupes on ``tool_invocation_id``. A received
        error response (``aiohttp.ClientResponseError`` from
        ``raise_for_status``) is NOT retried — that's a real answer from the
        server, not a lost request.
        """
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Hail-Signature": hmac_signing.sign(body, self._secret),
        }
        url = join_url(self._base_url, path)
        for attempt in range(2):
            try:
                session = self._get_session()
                async with session.post(url, data=body, headers=headers) as resp:
                    resp.raise_for_status()
                    return await resp.json()
            except (asyncio.TimeoutError, aiohttp.ClientConnectionError) as exc:
                if attempt == 1:
                    raise
                logger.warning(
                    "agent api post to %s failed (%s); retrying with same "
                    "tool_invocation_id",
                    path,
                    type(exc).__name__,
                )
        raise AssertionError("unreachable")

    async def aclose(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()


__all__ = ["AgentApiClient"]
