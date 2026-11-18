"""Optional PostHog MCP analytics for the HTTP server.

Off unless ``NEXT_PUBLIC_POSTHOG_KEY`` is set. Events carry tool names,
timings, outcomes, client names and the calling model. Argument values, results, error
messages and agent intent never leave the process: ``_keep_metadata`` drops
every property not on ``_KEPT_PROPERTIES``. In oauth-rs mode the caller's
user id (OAuth token or API key owner) becomes ``distinct_id``, the same
person id the website identifies, and the org id becomes the ``organization``
group. Only tool calls look the actor up (through the verified ``/whoami``);
analytics reads that cache and sets only email and name on the person profile.
Events before a bearer's first tool call stay anonymous. The only schema change
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
from hailhq.mcp.auth import AuthMode
from hailhq.mcp.discovery_auth import DISCOVERY_BEARER
from hailhq.mcp.tools import cached_actor, resolve_actor_for_analytics

logger = logging.getLogger(__name__)

try:
    from posthog import Posthog
    from posthog.mcp import get_request_headers, instrument
    from posthog.mcp.types import (
        MCPAnalyticsModelOptions,
        MCPAnalyticsOptions,
        UserIdentity,
    )
except ImportError:  # analytics must never block startup
    logger.warning("PostHog SDK import failed; MCP analytics is off", exc_info=True)
    Posthog = None
_FLUSH_SECONDS = 2

_KEPT_PROPERTIES = frozenset(
    {
        "$session_id",
        "$process_person_profile",
        "$groups",
        "$set",
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
_REQUEST_IDENTITY = "hail_request_identity"
# Set by instrument_analytics. Only oauth-rs requests carry a per-user bearer.
_per_user = False


async def _identify(_request: Any, extra: Any) -> UserIdentity | None:
    # Static-key mode has no per-request user; never identify an inbound header
    # as the operator of the server's shared key.
    if not _per_user:
        return None
    scheme, _, bearer = (
        (get_request_headers(extra) or {}).get("authorization") or ""
    ).partition(" ")
    bearer = bearer.strip()
    if scheme.lower() != "bearer" or not bearer or bearer == DISCOVERY_BEARER:
        return None
    # Cache only: a tool call resolves the actor before its event is built.
    # Handshake and list requests never wait on, or trigger, an API lookup.
    actor = cached_actor(bearer) or {}
    user_id = actor.get("user_id")
    if not user_id:
        return None
    properties = {
        key: value
        for key, value in {
            "email": actor.get("user_email"),
            "name": actor.get("user_name"),
        }.items()
        if isinstance(value, str) and value
    }
    org_id = actor.get("organization_id")
    return UserIdentity(
        distinct_id=str(user_id),
        properties=properties,
        groups={"organization": str(org_id)} if org_id else None,
    )


async def _event_identity(request: Any, extra: Any) -> dict[str, Any]:
    # Modern MCP has no transport session. The SDK can reuse one fallback
    # session across callers and merge their profiles. Stamp a fresh snapshot
    # on each event, read from the verified actor cache (no HTTP).
    identity = await _identify(request, extra)
    return {
        _REQUEST_IDENTITY: (
            {
                "distinct_id": identity.distinct_id,
                "properties": identity.properties,
                "groups": identity.groups,
            }
            if identity
            else None
        )
    }


def _keep_metadata(event: dict[str, Any]) -> dict[str, Any] | None:
    properties = event.get("properties") or {}
    if _REQUEST_IDENTITY not in properties:
        # The SDK swallows callback errors. Never emit its cached identity when
        # the request-specific enrichment failed; tool responses stay intact.
        return None
    # Ignore any sticky SDK identity; this event belongs to this request.
    identity = properties[_REQUEST_IDENTITY]
    properties = dict(properties)
    for key in ("$set", "$groups", "$process_person_profile"):
        properties.pop(key, None)
    if identity:
        event["distinct_id"] = identity["distinct_id"]
        if identity["properties"]:
            properties["$set"] = identity["properties"]
        if identity["groups"]:
            properties["$groups"] = identity["groups"]
    else:
        event["distinct_id"] = properties.get("$session_id") or "anonymous"
        properties["$process_person_profile"] = False
    event["properties"] = {k: v for k, v in properties.items() if k in _KEPT_PROPERTIES}
    if "$set" in event["properties"]:
        event["properties"]["$set"] = {
            k: v
            for k, v in event["properties"]["$set"].items()
            if k in {"email", "name"} and isinstance(v, str) and v
        }
    model = event["properties"].get("$mcp_llm_model")
    if model is not None and not (
        isinstance(model, str) and _MODEL_ID.fullmatch(model)
    ):
        for key in _MODEL_PROPERTIES:
            event["properties"].pop(key, None)
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


def instrument_analytics(server: Any, mode: AuthMode) -> Analytics | None:
    """Instrument ``server`` for tool-call analytics. ``None`` when off."""
    global _per_user
    resolve_actor_for_analytics(False)
    _per_user = mode is AuthMode.OAUTH_RS
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
            identify=_identify,
            event_properties=_event_identity,
            server_build=settings.logfire_service_version or None,
        )
        handle = instrument(server, client, options)
        resolve_actor_for_analytics(True)
        return Analytics(client, handle)
    except Exception:  # analytics must never block startup
        logger.warning("PostHog MCP analytics setup failed", exc_info=True)
        return None
