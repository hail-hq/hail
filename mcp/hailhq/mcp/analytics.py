"""Optional PostHog MCP analytics for the HTTP server.

Off unless ``MCP_POSTHOG_PROJECT_TOKEN`` is set. Events carry tool names,
timings, outcomes and client names only. Argument values, results, error
messages and agent intent never leave the process: ``_keep_metadata`` drops
every property not on ``_KEPT_PROPERTIES``. Tool schemas and tool results stay
unchanged, because the SDK options that inject arguments are turned off.
Any setup or capture failure logs a warning and the server keeps serving.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from hailhq.core.config import settings

logger = logging.getLogger(__name__)

_KEPT_PROPERTIES = frozenset(
    {
        "$session_id",
        "$process_person_profile",
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
        "$mcp_listed_tool_names",
        "$mcp_input_keys",
        "$mcp_duration_ms",
        "$mcp_is_error",
        "$mcp_error_type",
        "$mcp_error_status",
    }
)


def _keep_metadata(event: dict[str, Any]) -> dict[str, Any]:
    properties = event.get("properties") or {}
    event["properties"] = {k: v for k, v in properties.items() if k in _KEPT_PROPERTIES}
    return event


class Analytics:
    def __init__(self, client: Any, handle: Any) -> None:
        self._client = client
        self._handle = handle

    async def flush(self) -> None:
        try:
            await self._handle.flush()
            await asyncio.to_thread(self._client.shutdown)
        except Exception:  # noqa: BLE001 - analytics must never break shutdown
            logger.warning("PostHog MCP analytics flush failed", exc_info=True)


def instrument_analytics(server: Any) -> Analytics | None:
    """Instrument ``server`` before its HTTP app is built. ``None`` when off."""
    token = settings.mcp_posthog_project_token
    if not token:
        return None
    try:
        from posthog import Posthog
        from posthog.mcp import instrument
        from posthog.mcp.types import MCPAnalyticsOptions

        client = Posthog(token, host=settings.mcp_posthog_host)
        options = MCPAnalyticsOptions(
            context=False,
            enable_conversation_id=False,
            capture_model=False,
            enable_exception_autocapture=False,
            before_send=_keep_metadata,
            server_build=settings.logfire_service_version or None,
        )
        return Analytics(client, instrument(server, client, options))
    except Exception:  # noqa: BLE001 - analytics must never block startup
        logger.warning("PostHog MCP analytics setup failed", exc_info=True)
        return None
