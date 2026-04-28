"""MCP tool surface for Hail's outbound-call API.

Exposes four tools to the calling agent:

* ``place_call`` — originate an outbound phone call
* ``get_call`` — fetch the current state of one call
* ``list_calls`` — page through recent calls
* ``get_events`` — page through the event stream (call-narrow or org-wide)

The tool docstrings are the agent's only documentation, so each one
spells out the contract (required vs optional fields, mutually exclusive
modes, example invocation, terminal-status loop hint).

Errors are returned as ``{"error": "<message>"}`` dicts rather than
raised — agents read tool responses, not exception traces. Validation
that can be done locally (mode A/B exclusivity, ``<type>:<uuid>`` shape)
runs before any HTTP so a malformed call never hits the network.

The four tool functions are kept module-importable so unit tests can
call them directly with a constructed ``HailClient``; ``register_tools``
is the FastMCP wiring step. Nothing in this module holds module-level
state — the SDK (Task 11) and the MCP service therefore never share a
client by accident.
"""

from __future__ import annotations

import uuid
from typing import Any

from mcp.server.fastmcp import FastMCP

from hailhq.core.schemas import parse_resource_id

from hailhq.mcp.hail_client import HailAPIError, HailClient

# --------------------------------------------------------------------------- #
# Error mapping — turns HailAPIError into a stable agent-facing message.
# --------------------------------------------------------------------------- #


def _format_api_error(exc: HailAPIError) -> dict[str, Any]:
    status = exc.status
    if status == 401:
        return {"error": "auth failed: check HAIL_API_KEY"}
    if status == 404:
        return {"error": "call not found"}
    if status in (409, 422):
        return {"error": exc.detail}
    if 500 <= status < 600:
        return {"error": f"hail upstream error: {status}"}
    return {"error": f"hail api error {status}: {exc.detail}"}


# --------------------------------------------------------------------------- #
# Mode validation — mirrors cli/internal/cmd/call.go validateMode().
# --------------------------------------------------------------------------- #


_LLM_REQUIRED_KEYS = ("base_url", "api_key", "model")


def _validate_modes(
    system_prompt: str | None, llm: dict[str, Any] | None
) -> str | None:
    """Return an error message if mode A/B is misconfigured, else ``None``.

    Mirrors the CLI: exactly one of ``system_prompt`` / ``llm`` must be
    provided, and an ``llm`` dict must supply all three of ``base_url``,
    ``api_key``, ``model`` (non-empty strings).
    """
    has_prompt = bool(system_prompt)

    if has_prompt and llm is not None:
        return "system_prompt and llm are mutually exclusive (use one mode)"
    if not has_prompt and llm is None:
        return (
            "must provide either system_prompt or llm (with base_url, api_key, model)"
        )
    if llm is not None:
        missing = [
            k
            for k in _LLM_REQUIRED_KEYS
            if not isinstance(llm.get(k), str) or not llm[k]
        ]
        if missing:
            return (
                "llm requires non-empty base_url, api_key, and model "
                f"(missing: {', '.join(missing)})"
            )
        extra = set(llm) - set(_LLM_REQUIRED_KEYS)
        if extra:
            return f"llm has unexpected keys: {', '.join(sorted(extra))}"
    return None


# --------------------------------------------------------------------------- #
# Tool functions.
#
# Each returns a dict — either the raw API response on success, or
# ``{"error": ...}`` on a known failure mode. Type hints are deliberately
# concrete (no Pydantic models) because the MCP framework derives the
# tool's JSON schema from these annotations + the docstring.
# --------------------------------------------------------------------------- #


async def place_call(
    *,
    client: HailClient,
    to: str,
    system_prompt: str | None = None,
    llm: dict[str, Any] | None = None,
    from_: str | None = None,
    first_message: str | None = None,
    metadata: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    err = _validate_modes(system_prompt, llm)
    if err is not None:
        return {"error": err}
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    try:
        result = await client.place_call(
            to=to,
            system_prompt=system_prompt,
            llm=llm,
            from_=from_,
            first_message=first_message,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )
    except HailAPIError as exc:
        return _format_api_error(exc)
    # Surface the key in the response so the agent can replay this exact
    # request deterministically on a retry. ``setdefault`` so a future
    # server-side echo isn't clobbered.
    if isinstance(result, dict):
        result.setdefault("idempotency_key", idempotency_key)
    return result


async def get_call(*, client: HailClient, call_id: str) -> dict[str, Any]:
    try:
        return await client.get_call(call_id)
    except HailAPIError as exc:
        return _format_api_error(exc)


async def list_calls(
    *,
    client: HailClient,
    cursor: str | None = None,
    limit: int = 50,
    status: str | None = None,
    to: str | None = None,
) -> dict[str, Any]:
    try:
        return await client.list_calls(cursor=cursor, limit=limit, status=status, to=to)
    except HailAPIError as exc:
        return _format_api_error(exc)


async def get_events(
    *,
    client: HailClient,
    id: str | None = None,
    kind: str | None = None,
    cursor: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    if id is not None:
        try:
            parse_resource_id(id)
        except ValueError as exc:
            return {"error": str(exc)}
    try:
        return await client.get_events(id=id, kind=kind, cursor=cursor, limit=limit)
    except HailAPIError as exc:
        return _format_api_error(exc)


# --------------------------------------------------------------------------- #
# FastMCP registration.
#
# We register thin wrappers that close over ``client`` rather than the
# module-level functions directly — FastMCP derives the JSON schema from
# the *registered* function's signature, and we want the agent-facing
# signature to omit the ``client`` argument (it's an injected dep).
# --------------------------------------------------------------------------- #


def register_tools(mcp_app: FastMCP, client: HailClient) -> None:
    """Register the four Hail tools on a FastMCP app."""

    @mcp_app.tool(name="place_call")
    async def place_call_tool(
        to: str,
        system_prompt: str | None = None,
        llm: dict[str, Any] | None = None,
        from_: str | None = None,
        first_message: str | None = None,
        metadata: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Originate an outbound phone call.

        Provide either ``system_prompt`` (mode A — Hail's bundled
        fallback LLM uses this prompt) or ``llm`` (mode B — bring your
        own OpenAI-compatible endpoint as
        ``{"base_url": ..., "api_key": ..., "model": ...}``).
        Mode A and mode B are mutually exclusive; supply exactly one.

        ``to`` must be E.164 (e.g. ``+14155551234``). ``from_`` is
        optional and defaults to the first active number on your org.
        ``first_message`` is spoken on pickup before listening.
        ``metadata`` is free-form JSON attached to the call record.

        ``idempotency_key`` defaults to a fresh UUID per invocation
        and is returned in the response under ``idempotency_key`` — to
        retry *this* exact request (rather than dispatch a second call),
        pass the value back on the retry. A new key is a new call.

        Example:
            place_call(to="+14155551234",
                       system_prompt="You are scheduling a haircut.",
                       first_message="Hi, I'm calling on behalf of Alex.")

        Returns the API's ``CallResponse`` as a dict (id, status,
        from_e164, to_e164, ...). On failure returns
        ``{"error": "<message>"}`` instead.
        """
        return await place_call(
            client=client,
            to=to,
            system_prompt=system_prompt,
            llm=llm,
            from_=from_,
            first_message=first_message,
            metadata=metadata,
            idempotency_key=idempotency_key,
        )

    @mcp_app.tool(name="get_call")
    async def get_call_tool(call_id: str) -> dict[str, Any]:
        """Fetch the current state of one call by id.

        Use this after ``place_call`` (or to check on any prior call)
        to read the call's latest ``status`` and timing fields.

        Returns the API's ``CallResponse`` as a dict, or
        ``{"error": "call not found"}`` for an unknown id.
        """
        return await get_call(client=client, call_id=call_id)

    @mcp_app.tool(name="list_calls")
    async def list_calls_tool(
        cursor: str | None = None,
        limit: int = 50,
        status: str | None = None,
        to: str | None = None,
    ) -> dict[str, Any]:
        """List recent calls in your organization, newest first.

        Cursor-paginated: pass the previous response's ``next_cursor``
        to fetch the next page. ``status`` (one of queued, dialing,
        ringing, in_progress, completed, failed, busy, no_answer,
        canceled) and ``to`` (E.164) are optional server-side filters.

        Returns a dict ``{"items": [...], "next_cursor": <str|None>}``.
        """
        return await list_calls(
            client=client, cursor=cursor, limit=limit, status=status, to=to
        )

    @mcp_app.tool(name="get_events")
    async def get_events_tool(
        id: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Page through events from across the org or one resource.

        Pass ``id="call:<uuid>"`` to narrow to a single call; the
        response then includes a ``call_status`` field reflecting the
        call's current state. In v1 only the ``call`` resource type is
        supported. Without ``id``, returns events from across the
        whole org. ``kind`` filters server-side by event kind
        (``state_change``, ``agent_turn``, ``user_turn``, ``tool_call``,
        ``error``, ...).

        This is **not** a streaming subscription — the call returns
        whatever events exist now plus a ``next_cursor`` if more pages
        remain. To follow a call to completion, loop: pass the previous
        response's ``next_cursor`` until ``next_cursor`` is null and,
        when narrowed to a call, ``call_status`` is one of
        ``completed``, ``failed``, ``busy``, ``no_answer``, ``canceled``
        (the terminal set).

        Example:
            get_events(id="call:0c2f...-...", limit=200)

        Returns ``{"items": [...], "next_cursor": <str|None>,
        "call_status": <str|None>}`` on success, or
        ``{"error": ...}`` on a malformed ``id`` or upstream failure.
        """
        return await get_events(
            client=client, id=id, kind=kind, cursor=cursor, limit=limit
        )


__all__ = [
    "register_tools",
    "place_call",
    "get_call",
    "list_calls",
    "get_events",
]
