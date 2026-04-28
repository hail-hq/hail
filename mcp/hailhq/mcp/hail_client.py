"""Thin async httpx wrapper around the Hail API.

The MCP service talks to the same public ``POST /calls`` / ``GET /calls`` /
``GET /events`` surface that external clients use. This module is the
single place we encode that wire contract for the MCP tool layer:

* ``Authorization: Bearer <hail_api_key>`` is auto-injected on every request.
* ``Idempotency-Key`` is auto-injected on ``place_call`` (a fresh UUID per
  invocation unless the caller passed one explicitly), so an agent that
  retries a tool call deterministically replays the same originate.
* Non-2xx responses are mapped to a typed :class:`HailAPIError` with the
  status code + parsed ``detail`` field — the tool layer turns that into a
  structured ``{"error": ...}`` payload for the agent.

Configuration reads from :data:`hailhq.core.config.settings`; constructor
kwargs override for tests. The client is a regular ``httpx.AsyncClient``
holder — call :meth:`aclose` (or use it as an async context manager) to
release connections.
"""

from __future__ import annotations

import uuid
from typing import Any

import httpx

from hailhq.core.config import settings


class HailAPIError(Exception):
    """Non-2xx response from the Hail API.

    ``status`` is the HTTP status code; ``detail`` is the parsed
    ``detail`` field from the JSON body when present, otherwise the raw
    response text. The MCP tool layer converts this to an agent-facing
    error dict (see :mod:`hailhq.mcp.tools`).
    """

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"hail api error {status}: {detail}")
        self.status = status
        self.detail = detail


class HailClient:
    """Async httpx client for the Hail API."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = (base_url or settings.hail_api_url).rstrip("/")
        self._api_key = api_key if api_key is not None else settings.hail_api_key
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            transport=transport,
            headers={"Authorization": f"Bearer {self._api_key}"},
        )

    async def __aenter__(self) -> "HailClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ #
    # POST /calls
    # ------------------------------------------------------------------ #

    async def place_call(
        self,
        *,
        to: str,
        system_prompt: str | None = None,
        llm: dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """POST /calls — originate an outbound call.

        ``idempotency_key`` defaults to a fresh UUID4 so a retry of the
        same logical request deterministically replays rather than
        re-dispatching. The chosen key is *not* echoed in the API
        response body — the MCP tool layer surfaces it back to the
        agent (see :mod:`hailhq.mcp.tools`).
        """
        # Build the body with the wire-side ``"from"`` key (Pydantic alias).
        body: dict[str, Any] = {"to": to}
        if from_ is not None:
            body["from"] = from_
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if llm is not None:
            body["llm"] = llm
        if first_message is not None:
            body["first_message"] = first_message
        if metadata is not None:
            body["metadata"] = metadata

        headers = {
            "Idempotency-Key": idempotency_key or str(uuid.uuid4()),
        }
        resp = await self._client.post("/calls", json=body, headers=headers)
        return _decode(resp)

    # ------------------------------------------------------------------ #
    # GET /calls/{id}
    # ------------------------------------------------------------------ #

    async def get_call(self, call_id: str) -> dict[str, Any]:
        resp = await self._client.get(f"/calls/{call_id}")
        return _decode(resp)

    # ------------------------------------------------------------------ #
    # GET /calls
    # ------------------------------------------------------------------ #

    async def list_calls(
        self,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        if status is not None:
            params["status"] = status
        if to is not None:
            params["to"] = to
        resp = await self._client.get("/calls", params=params)
        return _decode(resp)

    # ------------------------------------------------------------------ #
    # GET /events
    # ------------------------------------------------------------------ #

    async def get_events(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {}
        if id is not None:
            params["id"] = id
        if kind is not None:
            params["kind"] = kind
        if cursor is not None:
            params["cursor"] = cursor
        if limit is not None:
            params["limit"] = limit
        resp = await self._client.get("/events", params=params)
        return _decode(resp)


def _decode(resp: httpx.Response) -> dict[str, Any]:
    """Return the JSON body on 2xx, raise :class:`HailAPIError` otherwise."""
    if 200 <= resp.status_code < 300:
        return resp.json()
    detail: str
    try:
        payload = resp.json()
    except ValueError:
        detail = resp.text or resp.reason_phrase
    else:
        if isinstance(payload, dict) and "detail" in payload:
            d = payload["detail"]
            detail = d if isinstance(d, str) else str(d)
        else:
            detail = str(payload)
    raise HailAPIError(status=resp.status_code, detail=detail)


__all__ = ["HailAPIError", "HailClient"]
