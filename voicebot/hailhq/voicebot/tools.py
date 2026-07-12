"""Adapt the core agent-tool registry to LiveKit function tools.

Per-call filtering: the dispatch metadata's ``tools`` list (None ⇒ all,
[] ⇒ none) then each spec's per-org ``is_available`` check. Send tools
require HAIL_INTERNAL_SECRET (they call the API's internal agent routes);
without it only local/read-only tools remain.

A tool failure never kills the call: the wrapper catches everything and
returns a speakable apology to the LLM.
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

from livekit.agents.llm import function_tool
from livekit.agents.voice import RunContext

from hailhq.core.agent_tools.client import AgentApiClient
from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.config import settings
from hailhq.core.db import session_scope

logger = logging.getLogger("hailhq.voicebot")

SPOKEN_TOOL_FAILURE = "Sorry, that didn't work."


def _make_handler(spec: ToolSpec, tctx: ToolContext):
    async def handler(raw_arguments: dict[str, Any], context: RunContext) -> str:
        try:
            # session_control tools (end_call) must not cut off the agent's
            # own goodbye: wait for the pre-tool speech to finish playing.
            if spec.risk_tier == "session_control":
                await context.wait_for_playout()
            return await spec.execute(tctx, raw_arguments)
        except Exception:
            logger.exception(
                "agent tool %s failed for call_id=%s", spec.name, tctx.call_id
            )
            return SPOKEN_TOOL_FAILURE

    return handler


def _wrap(spec: ToolSpec, tctx: ToolContext):
    return function_tool(
        _make_handler(spec, tctx),
        raw_schema={
            "name": spec.name,
            "description": spec.description,
            "parameters": spec.parameters,
        },
    )


async def build_agent_tools(
    metadata: dict[str, Any], *, call_id: UUID, hangup
) -> tuple[list, AgentApiClient | None]:
    """Build this call's LiveKit tools. Returns (tools, api_client).

    The caller must ``aclose()`` the client at shutdown. Returns no tools
    when the dispatch predates the ``organization_id`` field (rolling
    deploy) — no tools beats wrong-org tools.
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
    specs = [s for s in all_tools() if allowed is None or s.name in allowed]
    if not specs:
        return [], None

    api: AgentApiClient | None = None
    if settings.hail_internal_secret:
        api = AgentApiClient(settings.hail_api_url, settings.hail_internal_secret)

    tctx = ToolContext(
        call_id=call_id, organization_id=organization_id, api=api, hangup=hangup
    )

    available: list[ToolSpec] = []
    async with session_scope() as session:
        for spec in specs:
            try:
                if await spec.is_available(organization_id, session):
                    available.append(spec)
            except Exception:
                logger.exception("availability check failed for tool %s", spec.name)
                # Load-bearing: a failed statement leaves the shared session's
                # transaction aborted; without this rollback every later
                # is_available check raises PendingRollbackError and gets
                # swallowed too, silently disabling available tools.
                await session.rollback()

    return [_wrap(s, tctx) for s in available], api


__all__ = ["SPOKEN_TOOL_FAILURE", "build_agent_tools"]
