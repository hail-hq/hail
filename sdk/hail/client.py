"""Hail SDK public client.

Usage::

    from hail import Client

    async with Client(api_key="sk-...") as client:
        call = await client.calls.create(
            to="+15551234567",
            system_prompt="You are calling to confirm a reschedule.",
        )
        async for event in client.events.tail(id=f"call:{call.id}"):
            print(event)

The client is async-only. There is no sync facade in v1; build one on top
with ``asyncio.run`` if you need it.
"""

from __future__ import annotations

import asyncio
import base64
import os
from datetime import datetime
from typing import Any, AsyncIterator
from uuid import UUID

import httpx

from hail._errors import HailConfigError
from hail._http import _HailHTTP, generate_idempotency_key
from hail._resource_id import parse_resource_id
from hail.models import (
    CallEventResponse,
    CallListResponse,
    CallResponse,
    CallStatus,
    EventStreamResponse,
    LLMConfig,
    TERMINAL_CALL_STATUSES,
)

_DEFAULT_BASE_URL = "https://api.hail.so"
_TAIL_PAGE_SIZE = 1000


def _encode_event_cursor(occurred_at: datetime, event_id: UUID) -> str:
    """Base64-urlsafe (no padding) of ``"<isoformat>|<uuid>"``.

    Mirrors ``hailhq.core.schemas.encode_cursor`` byte-for-byte; the SDK
    can't import core, so the logic is duplicated. Used to synthesize the
    next polling cursor when the server didn't hand one back.
    """
    raw = f"{occurred_at.isoformat()}|{event_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class _CallsResource:
    """``client.calls.*`` — POST/GET/LIST against ``/calls``."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def create(
        self,
        *,
        to: str,
        system_prompt: str | None = None,
        llm: LLMConfig | dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> CallResponse:
        """Originate an outbound call.

        Exactly one of ``system_prompt`` (mode A) or a fully-populated
        ``llm`` block (mode B) must be provided — server enforces this with
        a 422; we don't pre-validate so SDK and API stay in lockstep on the
        rule. ``idempotency_key`` defaults to a fresh UUIDv4.
        """
        body: dict[str, Any] = {"to": to}
        if from_ is not None:
            body["from"] = from_
        if system_prompt is not None:
            body["system_prompt"] = system_prompt
        if first_message is not None:
            body["first_message"] = first_message
        if metadata is not None:
            body["metadata"] = metadata
        if llm is not None:
            body["llm"] = llm.model_dump() if isinstance(llm, LLMConfig) else llm

        key = idempotency_key or generate_idempotency_key()
        data = await self._http.request(
            "POST",
            "/calls",
            json=body,
            headers={"Idempotency-Key": key},
        )
        return CallResponse.model_validate(data)

    async def get(self, call_id: str | UUID) -> CallResponse:
        """Fetch a single call by id."""
        cid = str(call_id)
        data = await self._http.request("GET", f"/calls/{cid}")
        return CallResponse.model_validate(data)

    async def list(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        status: CallStatus | None = None,
        to: str | None = None,
    ) -> CallListResponse:
        """Cursor-paginated list, scoped to the caller's organization."""
        params = {"limit": limit, "cursor": cursor, "status": status, "to": to}
        data = await self._http.request("GET", "/calls", params=params)
        return CallListResponse.model_validate(data)


class _EventsResource:
    """``client.events.*`` — list and tail against ``/events``."""

    def __init__(self, http: _HailHTTP) -> None:
        self._http = http

    async def list(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> EventStreamResponse:
        """One-shot list (matches ``GET /events`` exactly)."""
        if id is not None:
            # Validate locally first so a typo fails before any HTTP. The wire
            # form is the original ``<type>:<uuid>`` string — we don't rebuild
            # it from the parsed pieces.
            parse_resource_id(id)
        return await self._fetch_page(id=id, kind=kind, cursor=cursor, limit=limit)

    async def tail(
        self,
        *,
        id: str | None = None,
        kind: str | None = None,
        interval_seconds: float = 0.5,
        follow: bool = True,
    ) -> AsyncIterator[CallEventResponse]:
        """Yield events as they arrive.

        Mirrors the CLI's tail loop (``cli/internal/cmd/tail.go``):
          * Drains all inner pages while the server reports ``next_cursor``.
          * After draining, synthesizes the next polling cursor from the last
            seen event's ``(occurred_at, id)`` — needed because the API only
            sets ``next_cursor`` when ``len(rows) > limit``.
          * When ``id`` resolves to a call (``call:<uuid>``), exits cleanly
            once ``call_status`` reaches a terminal value.
          * ``follow=False`` makes it stop after the first page (CLI's
            ``--no-follow``).
        """
        single_call = False
        if id is not None:
            type_str, _ = parse_resource_id(id)
            single_call = type_str == "call"

        cursor: str | None = None
        while True:
            page_resp = await self._fetch_page(
                id=id, kind=kind, cursor=cursor, limit=_TAIL_PAGE_SIZE
            )
            last_event: CallEventResponse | None = None
            page = page_resp
            while True:
                for ev in page.items:
                    last_event = ev
                    yield ev
                if page.next_cursor:
                    cursor = page.next_cursor
                    page = await self._fetch_page(
                        id=id, kind=kind, cursor=cursor, limit=_TAIL_PAGE_SIZE
                    )
                else:
                    break
            # Synthesize forward cursor for the next outer poll.
            if last_event is not None:
                cursor = _encode_event_cursor(last_event.occurred_at, last_event.id)

            if not follow:
                return
            if (
                single_call
                and page_resp.call_status is not None
                and page_resp.call_status in TERMINAL_CALL_STATUSES
            ):
                return

            await asyncio.sleep(interval_seconds)

    async def _fetch_page(
        self,
        *,
        id: str | None,
        kind: str | None,
        cursor: str | None,
        limit: int,
    ) -> EventStreamResponse:
        params = {"limit": limit, "id": id, "kind": kind, "cursor": cursor}
        data = await self._http.request("GET", "/events", params=params)
        return EventStreamResponse.model_validate(data)


class Client:
    """Hail API client.

    ``api_key`` defaults to ``$HAIL_API_KEY``; ``base_url`` defaults to
    ``$HAIL_API_URL`` and then to ``https://api.hail.so``. Construction
    raises :class:`HailConfigError` if no API key is discoverable.

    The underlying ``httpx.AsyncClient`` is built lazily on first request
    and torn down by :meth:`aclose` (or ``__aexit__``).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: httpx.Timeout | float | None = None,
        _transport_client: httpx.AsyncClient | None = None,
    ) -> None:
        resolved_key = (
            api_key if api_key is not None else os.environ.get("HAIL_API_KEY")
        )
        if not resolved_key:
            raise HailConfigError(
                "no api_key provided; pass api_key= or set HAIL_API_KEY"
            )
        resolved_base = (
            base_url
            if base_url is not None
            else os.environ.get("HAIL_API_URL", _DEFAULT_BASE_URL)
        )

        self._http = _HailHTTP(
            base_url=resolved_base,
            api_key=resolved_key,
            timeout=timeout,
            transport_client=_transport_client,
        )
        self.calls = _CallsResource(self._http)
        self.events = _EventsResource(self._http)
        self.base_url = resolved_base.rstrip("/")

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "Client":
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.aclose()

    def __repr__(self) -> str:
        return f"<hail.Client base_url={self.base_url!r}>"


__all__ = ["Client"]
