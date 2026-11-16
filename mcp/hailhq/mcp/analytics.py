"""Optional PostHog MCP analytics for the HTTP server.

Off unless ``NEXT_PUBLIC_POSTHOG_KEY`` is set. Events carry tool names,
timings, outcomes, client names and the calling model only. Argument values, results, error
messages and agent intent never leave the process: ``_keep_metadata`` drops
every property not on ``_KEPT_PROPERTIES``. In oauth-rs mode the caller's
user id (OAuth token or API key owner) becomes ``distinct_id``, the same
person id the website identifies, and the org id becomes the ``organization``
group. Both come from the actor cache tool calls fill. The only schema change
is the SDK's optional ``llm_model`` argument, which the SDK removes before the
tool runs. Tool results stay unchanged.
Any setup or capture failure logs a warning and the server keeps serving.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from hailhq.core.config import settings
from hailhq.mcp.tools import cached_actor_ids, resolve_actor_for_analytics

logger = logging.getLogger(__name__)

try:
    from posthog import Posthog
    from posthog.mcp import get_request_headers, instrument
    from posthog.mcp.types import MCPAnalyticsModelOptions, MCPAnalyticsOptions
except ImportError:  # analytics must never block startup
    logger.warning("PostHog SDK import failed; MCP analytics is off", exc_info=True)
    Posthog = None
_FLUSH_SECONDS = 2

_KEPT_PROPERTIES = frozenset(
    {
        "$session_id",
        "$process_person_profile",
        "$groups",
        "$mcp_interface",
        "$mcp_source",
        "$mcp_server_name",
        "$mcp_server_version",
        "$mcp_server_build",
        "$mcp_client_name",
        "$mcp_client_version",
        "$mcp_client_user_agent",
        "$mcp_vendor_client",
        "$mcp_protocol_version",
        "$mcp_tool_name",
        "$mcp_resource_name",
        "$mcp_tool_category",
        "$mcp_llm_model",
        "$mcp_llm_model_source",
        "$mcp_listed_tool_names",
        "$mcp_input_keys",
        "$mcp_duration_ms",
        "$mcp_is_error",
        "$mcp_error_type",
        "$mcp_error_status",
    }
)


# Shown on every tool's optional llm_model input. Hail's description rules
# apply: it states what the field is and gives no instruction.
_MODEL_DESCRIPTION = (
    "Model ID of the calling agent. Optional. Recorded for analytics only."
)
# The agent types llm_model, so it is free text. Keep it only when it looks
# like a model ID (has a letter, no spaces or @), never an email or a number.
_MODEL_ID = re.compile(r"(?=[^@\s]*[A-Za-z])[A-Za-z0-9][A-Za-z0-9._:/+-]{0,99}")
_MODEL_PROPERTIES = ("$mcp_llm_model", "$mcp_llm_model_source")

_ORG_PROPERTY = "hail_organization_id"
_USER_PROPERTY = "hail_user_id"


def _actor(_request: Any, extra: Any) -> dict[str, str] | None:
    scheme, _, bearer = (
        (get_request_headers(extra) or {}).get("authorization") or ""
    ).partition(" ")
    if scheme.lower() != "bearer" or not bearer.strip():
        return None
    org_id, user_id = cached_actor_ids(bearer.strip())
    ids = {_ORG_PROPERTY: org_id, _USER_PROPERTY: user_id}
    return {k: v for k, v in ids.items() if v} or None


def _keep_metadata(event: dict[str, Any]) -> dict[str, Any]:
    properties = event.get("properties") or {}
    org_id = properties.get(_ORG_PROPERTY)
    user_id = properties.get(_USER_PROPERTY)
    event["properties"] = {k: v for k, v in properties.items() if k in _KEPT_PROPERTIES}
    model = event["properties"].get("$mcp_llm_model")
    if model is not None and not (
        isinstance(model, str) and _MODEL_ID.fullmatch(model)
    ):
        for key in _MODEL_PROPERTIES:
            event["properties"].pop(key, None)
    if user_id or org_id:
        event["distinct_id"] = user_id or org_id
    if org_id:
        event["properties"]["$groups"] = {"organization": org_id}
    if user_id:
        # The website identifies the same user id with email and name, so the
        # event joins that person. Nothing personal is set from here.
        event["properties"]["$process_person_profile"] = True
    return event


class Analytics:
    def __init__(self, client: Any, handle: Any) -> None:
        self._client = client
        self._handle = handle

    async def flush(self) -> None:
        """Bounded: ``Posthog.shutdown()`` waits with no limit while PostHog is down."""
        try:
            await asyncio.wait_for(self._handle.flush(), _FLUSH_SECONDS)
        except Exception:  # analytics must never break shutdown
            logger.warning("PostHog MCP capture drain failed", exc_info=True)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._client.flush, _FLUSH_SECONDS), _FLUSH_SECONDS
            )
        except Exception:  # analytics must never break shutdown
            logger.warning("PostHog MCP client flush failed", exc_info=True)


def instrument_analytics(server: Any) -> Analytics | None:
    """Instrument ``server`` for tool-call analytics. ``None`` when off."""
    resolve_actor_for_analytics(False)
    token = settings.next_public_posthog_key
    if not token or Posthog is None:
        return None
    try:
        client = Posthog(token, host=settings.next_public_posthog_host or None)
        options = MCPAnalyticsOptions(
            context=False,
            enable_conversation_id=False,
            enable_exception_autocapture=False,
            capture_model=MCPAnalyticsModelOptions(description=_MODEL_DESCRIPTION),
            before_send=_keep_metadata,
            event_properties=_actor,
            server_build=settings.logfire_service_version or None,
        )
        handle = instrument(server, client, options)
        resolve_actor_for_analytics(True)
        return Analytics(client, handle)
    except Exception:  # analytics must never block startup
        logger.warning("PostHog MCP analytics setup failed", exc_info=True)
        return None
