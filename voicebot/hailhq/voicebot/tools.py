"""Adapt the core agent-tool registry to LiveKit function tools.

Per-call filtering: the dispatch metadata's ``tools`` list (None ⇒ all,
[] ⇒ only the tools every call keeps; HTTP tools stay on) then each spec's
per-org ``is_available`` check. Send tools
require HAIL_INTERNAL_SECRET (they call the API's internal agent routes);
without it only local/read-only tools remain.

A tool failure never kills the call: the wrapper catches everything and
returns a speakable apology to the LLM.
"""

from __future__ import annotations

import logging
import math
import time
from typing import Any
from uuid import UUID

from hailhq.core.agent_tools import transfer_call
from hailhq.core.agent_tools.client import AgentApiClient
from hailhq.core.agent_tools.http_tool import Conversation, load_http_tool_specs
from hailhq.core.agent_tools.registry import call_tools
from hailhq.core.agent_tools.spec import SPOKEN_FALLBACK, ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.voicebot.attachment_reader import inspect_tool_file
from livekit.agents.llm import function_tool
from livekit.agents.voice import RunContext

logger = logging.getLogger("hailhq.voicebot")

SPOKEN_TOOL_FAILURE = SPOKEN_FALLBACK
# end_call's answer when the other party talked over the goodbye.
STILL_TALKING = (
    "They started talking during your goodbye, so the call is still open. "
    "Do not hang up: listen and answer them."
)


# Speech this long keeps the call open after a goodbye. A quick "bye" or "ok"
# over the goodbye is shorter; "What is that?" is longer.
KEEP_CALL_SPEECH_SECONDS = 1.0


class SpeechTracker:
    """When the other party talks (time.monotonic()), fed by the session's
    ``user_state_changed`` events."""

    def __init__(self) -> None:
        self._started: float | None = None
        self._long_ended = -math.inf

    def on_state(self, state: str | None, now: float) -> None:
        if state == "speaking":
            if self._started is None:
                self._started = now
        elif self._started is not None:
            if now - self._started >= KEEP_CALL_SPEECH_SECONDS:
                self._long_ended = now
            self._started = None

    def talked_since(self, since: float, now: float) -> bool:
        """True when they talk now for long enough, or did so after
        ``since``. Short words count for neither."""
        if (
            self._started is not None
            and now - self._started >= KEEP_CALL_SPEECH_SECONDS
        ):
            return True
        return self._long_ended >= since


def _make_handler(spec: ToolSpec, tctx: ToolContext):
    async def handler(raw_arguments: dict[str, Any], context: RunContext) -> str | None:
        try:
            if spec.uninterruptible:
                # "Okay, thanks" over "I'm connecting you" must not cancel
                # the transfer before it dials.
                context.disallow_interruptions()
            # session_control tools (end_call) must not cut off the agent's
            # own goodbye: wait for the pre-tool speech to finish playing.
            # Someone who starts talking during the goodbye is not done: keep
            # the call. Only end_call: DTMF and transfers run over IVR menus
            # and "okay"s, so speech must not cancel them.
            if spec.risk_tier == "session_control" and spec.waits_for_speech:
                started = time.monotonic()
                await context.wait_for_playout()
                if (
                    spec.name == "end_call"
                    and tctx.spoke_since is not None
                    and tctx.spoke_since(started)
                ):
                    logger.info(
                        "call_id=%s end_call skipped: still talking", tctx.call_id
                    )
                    return STILL_TALKING
            if spec.wait_message:
                # Not awaited: the request runs while the sentence plays. A
                # session that cannot speak still runs the tool.
                try:
                    context.session.say(spec.wait_message)
                except Exception:
                    logger.warning(
                        "call_id=%s wait message for %s not said",
                        tctx.call_id,
                        spec.name,
                    )
            # None means no reply turn (a transfer that started ringing).
            result = await spec.execute(tctx, raw_arguments)
            if spec.name in ("read_email", "read_browser_inbox"):
                result = await inspect_tool_file(result, context.session.llm)
            return result
        except Exception:
            logger.exception(
                "agent tool %s failed for call_id=%s", spec.name, tctx.call_id
            )
            return SPOKEN_TOOL_FAILURE

    return handler


def wrap_tool(spec: ToolSpec, tctx: ToolContext):
    """One core tool as a LiveKit function tool bound to ``tctx``."""
    return function_tool(
        _make_handler(spec, tctx),
        raw_schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    )


async def _http_tools(
    metadata: dict[str, Any], call_id: UUID, organization_id: UUID
) -> list[ToolSpec]:
    """The saved agent's HTTP tools. A failure here loses only these, never
    the built-in tools."""
    raw_agent = metadata.get("agent_id")
    if not raw_agent:
        return []  # a call placed with an inline prompt has no saved agent
    try:
        agent_id = UUID(str(raw_agent))
    except ValueError:
        logger.warning("call_id=%s malformed agent_id; no http tools", call_id)
        return []
    return await load_http_tool_specs(
        Conversation(
            channel="call",
            id=call_id,
            agent_id=agent_id,
            organization_id=organization_id,
        )
    )


def every_call_tools(
    metadata: dict[str, Any], *, call_id: UUID, hangup, spoke_since=None
) -> list:
    """The tools every call keeps (``ToolSpec.on_every_call``), for a call
    whose other tools could not be built. They need no database or API, so
    a call can still hang up when those fail. Empty when the dispatch has no
    valid ``organization_id``."""
    try:
        organization_id = UUID(str(metadata.get("organization_id")))
    except ValueError:
        return []
    tctx = ToolContext(
        call_id=call_id,
        organization_id=organization_id,
        api=None,
        hangup=hangup,
        send_dtmf=None,
        spoke_since=spoke_since,
    )
    return [wrap_tool(s, tctx) for s in call_tools() if s.on_every_call]


async def build_agent_tools(
    metadata: dict[str, Any],
    *,
    call_id: UUID,
    hangup,
    send_dtmf,
    bridge=None,
    spoke_since=None,
    cancel_bridge=None,
    prompt=None,
) -> tuple[list, AgentApiClient | None]:
    """Build this call's LiveKit tools. Returns (tools, api_client).

    The caller must ``aclose()`` the client at shutdown. Returns no tools
    when the dispatch predates the ``organization_id`` field (rolling
    deploy): no tools beats wrong-org tools.
    """
    raw_org = metadata.get("organization_id")
    if raw_org is None:
        logger.warning("dispatch metadata has no organization_id; agent tools disabled")
        return [], None
    try:
        organization_id = UUID(str(raw_org))
    except ValueError:
        logger.warning(
            "dispatch metadata has malformed organization_id; agent tools disabled"
        )
        return [], None

    allowed = metadata.get("tools")  # None ⇒ all available
    if allowed is not None and not isinstance(allowed, list):
        # Malformed dispatch metadata (e.g. a bare string instead of a
        # list): `s.name in allowed` would silently do substring
        # matching on a string instead of list membership. Fail closed,
        # matching the malformed-organization_id posture above.
        logger.warning(
            "dispatch metadata 'tools' is not a list (%r); only every-call tools",
            type(allowed).__name__,
        )
        return (
            every_call_tools(
                metadata, call_id=call_id, hangup=hangup, spoke_since=spoke_since
            ),
            None,
        )
    specs: list[ToolSpec] = []
    for s in call_tools():
        # A tool every call keeps ignores the `tools` list.
        if allowed is not None and s.name not in allowed and not s.on_every_call:
            continue
        if s.bind is not None:
            # Per-call tools (transfer_call) shape themselves from the
            # dispatch metadata, or drop out when it has nothing for them.
            bound = s.bind(metadata)
            if bound is None:
                continue
            s = bound
        specs.append(s)
        if s.name == transfer_call.SPEC.name:
            # Its dial runs in the background: cancel_transfer stops the ring.
            # It comes with transfer_call, so a `tools` list never names it.
            specs.append(transfer_call.CANCEL_SPEC)
    # The agent's own HTTP tools are not built-ins: a `tools` list does not
    # name them, and they stay on with `tools: []`.
    specs.extend(await _http_tools(metadata, call_id, organization_id))
    # Every-call tools need no database or API: a call with only those
    # (`tools: []`) opens neither.
    checked = [s for s in specs if not s.on_every_call]
    api: AgentApiClient | None = None
    if checked and settings.hail_internal_secret:
        api = AgentApiClient(settings.hail_api_url, settings.hail_internal_secret)

    tctx = ToolContext(
        call_id=call_id,
        organization_id=organization_id,
        api=api,
        hangup=hangup,
        send_dtmf=send_dtmf,
        bridge=bridge,
        spoke_since=spoke_since,
        cancel_bridge=cancel_bridge,
        prompt=prompt,
    )

    passed: set[str] = set()
    try:
        await _check_availability(checked, organization_id, passed)
    except BaseException:
        # The caller gets no client on a failure, so close it here.
        if api is not None:
            await api.aclose()
        raise

    available = [s for s in specs if s.on_every_call or s.name in passed]
    return [wrap_tool(s, tctx) for s in available], api


async def _check_availability(
    checked: list[ToolSpec], organization_id: UUID, passed: set[str]
) -> None:
    """Add to ``passed`` the name of each spec available to the org."""
    if checked:
        async with session_scope() as session:
            for spec in checked:
                try:
                    if await spec.is_available(organization_id, session):
                        passed.add(spec.name)
                except Exception:
                    logger.exception("availability check failed for tool %s", spec.name)
                    # Load-bearing: a failed statement leaves the shared
                    # session's transaction aborted; without this rollback
                    # every later is_available check raises
                    # PendingRollbackError and gets swallowed too, silently
                    # disabling available tools.
                    await session.rollback()


__all__ = ["SPOKEN_TOOL_FAILURE", "build_agent_tools", "every_call_tools", "wrap_tool"]
