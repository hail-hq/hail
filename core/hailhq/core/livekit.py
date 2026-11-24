"""LiveKit Cloud integration helpers.

Thin async wrappers around ``livekit-api`` that keep three concerns in one
place so neither the API service nor the voicebot worker has to reimplement
them:

* room provisioning (``create_room``)
* explicit agent dispatch (``dispatch_agent``)
* outbound SIP dial through the configured trunk (``create_sip_participant``)

Field names were verified against the canonical protobuf definitions in
``livekit/protocol`` (``livekit_agent_dispatch.proto`` and
``livekit_sip.proto``) and the public reference at
``docs.livekit.io/reference/python/livekit/api/`` against ``livekit-api``
v1.1 (December 2025).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.telemetry import telemetry_enabled
from hailhq.core.telemetry_identity import get_identity
from livekit import api
from livekit.protocol.models import ListUpdate
from livekit.protocol.webhook import WebhookEvent
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

VOICE_READY_ATTRIBUTE = "hail.voice.ready"

# A connection's inbound trunk lists this placeholder as its only number. LiveKit
# treats an empty ``numbers`` list as "any number", so it must never be removed.
PBX_TRUNK_PREFIX = "pbx-"

_PBX_TRANSPORTS = {
    "auto": api.SIPTransport.SIP_TRANSPORT_AUTO,
    "udp": api.SIPTransport.SIP_TRANSPORT_UDP,
    "tcp": api.SIPTransport.SIP_TRANSPORT_TCP,
    "tls": api.SIPTransport.SIP_TRANSPORT_TLS,
}


def pbx_placeholder(connection_id: UUID) -> str:
    """The only number on a connection's inbound trunk: ``pbx-<connection id>``."""
    return f"{PBX_TRUNK_PREFIX}{connection_id}"


class WorkerGaveUp(Exception):
    """The worker finalized the call and left before signalling readiness."""


def verify_webhook_event(body: str, auth_token: str) -> WebhookEvent:
    """Parse a LiveKit webhook body after checking its signature.

    Raises on a missing, forged or stale ``Authorization`` token.
    """
    receiver = api.WebhookReceiver(
        api.TokenVerifier(settings.livekit_api_key, settings.livekit_api_secret)
    )
    return receiver.receive(body, auth_token)


class LiveKitClient:
    """Async helper bundle backed by a single ``livekit.api.LiveKitAPI``.

    Construct once per process (the underlying client owns an aiohttp
    session); call :meth:`aclose` on shutdown.
    """

    def __init__(
        self,
        url: str | None = None,
        api_key: str | None = None,
        api_secret: str | None = None,
    ) -> None:
        self._args = (
            url or settings.livekit_url,
            api_key or settings.livekit_api_key,
            api_secret or settings.livekit_api_secret,
        )
        self._lkapi = api.LiveKitAPI(*self._args)
        # Made on first use: a client that never replays a request to
        # another LiveKit region (see ``create_sip_participant``).
        self._lkapi_once: api.LiveKitAPI | None = None

    async def aclose(self) -> None:
        """Close the underlying aiohttp sessions.

        Safe to call multiple times; ``LiveKitAPI.aclose`` is idempotent.
        """
        await self._lkapi.aclose()
        if self._lkapi_once is not None:
            await self._lkapi_once.aclose()

    async def list_egress(self, egress_id: str) -> list[api.EgressInfo]:
        """Current state of one Egress, or ``[]`` when LiveKit no longer has it."""
        result = await self._lkapi.egress.list_egress(
            api.ListEgressRequest(egress_id=egress_id)
        )
        return list(result.items)

    async def create_room(self, call_id: UUID) -> str:
        """Provision a LiveKit room named ``hail-<call_id>``.

        Using the call id as the room name keeps room <-> call traceability
        without an extra mapping table. Returns the room name.
        """
        room_name = f"hail-{call_id}"
        await self._lkapi.room.create_room(api.CreateRoomRequest(name=room_name))
        return room_name

    async def dispatch_agent(
        self,
        room_name: str,
        agent_name: str,
        metadata: dict,
    ) -> str:
        """Explicitly dispatch the named voicebot agent into ``room_name``.

        ``metadata`` is serialized to a JSON string because the underlying
        proto field (``CreateAgentDispatchRequest.metadata``) is ``string``.
        The voicebot worker (Task 8) must register with
        ``WorkerOptions(agent_name=...)`` for explicit dispatch to bind.

        Returns the dispatch id (``AgentDispatch.id``) for traceability /
        logging.
        """
        metadata = dict(metadata)
        if telemetry_enabled():
            carrier: dict[str, str] = {}
            TraceContextTextMapPropagator().inject(carrier)
            metadata["hail_trace_context"] = carrier
            # Ids only: names and emails stay out of LiveKit; the voicebot looks them up.
            metadata["hail_actor_identity"] = {
                k: v
                for k, v in get_identity().items()
                if k not in {"user_email", "organization_name"}
            }
        result = await self._lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name=agent_name,
                metadata=json.dumps(metadata),
            )
        )
        return result.id

    async def delete_dispatch(self, dispatch_id: str, room_name: str) -> None:
        """Delete a previously-created explicit agent dispatch."""
        await self._lkapi.agent_dispatch.delete_dispatch(dispatch_id, room_name)

    async def delete_room(self, room_name: str) -> None:
        """Delete a room and disconnect any participants still inside it."""
        await self._lkapi.room.delete_room(api.DeleteRoomRequest(room=room_name))

    async def create_sip_participant(
        self,
        room_name: str,
        to_e164: str,
        from_e164: str,
        sip_trunk_id: str,
        participant_identity: str,
        headers: dict[str, str] | None = None,
        wait_until_answered: bool = False,
        ringing_timeout_seconds: int | None = None,
        failover: bool = True,
    ) -> api.SIPParticipantInfo:
        """Place an outbound SIP call into ``room_name`` via LiveKit.

        Per ``livekit_sip.proto``:

        * ``sip_call_to`` is the **destination** (the number being dialed).
        * ``sip_number`` is the **caller-ID / from** number presented to
          the callee. When empty, the trunk's configured number is used.

        LiveKit's SIP service issues the INVITE through the trunk pointed to
        by ``sip_trunk_id`` (which in v1 is a Twilio Elastic SIP Trunk).

        ``failover=False`` sends the request once, never to another region:
        an error then belongs to that one attempt (the SDK otherwise replays
        a request after a timeout or a 5xx and raises only the last error).
        """
        request = api.CreateSIPParticipantRequest(
            sip_trunk_id=sip_trunk_id,
            sip_call_to=to_e164,
            sip_number=from_e164,
            room_name=room_name,
            participant_identity=participant_identity,
            participant_name=participant_identity,
            headers=headers or {},
            wait_until_answered=wait_until_answered,
        )
        if ringing_timeout_seconds is not None:
            request.ringing_timeout.seconds = ringing_timeout_seconds
        lkapi = self._lkapi
        if not failover:
            if self._lkapi_once is None:
                self._lkapi_once = api.LiveKitAPI(*self._args, failover=False)
            lkapi = self._lkapi_once
        return await lkapi.sip.create_sip_participant(request)

    async def wait_for_agent_ready(
        self,
        room_name: str,
        *,
        timeout: float = 20.0,
        poll_interval: float = 0.25,
        gave_up: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        """Do not dial until the worker's session and AMD audio input are armed.

        A dispatch response only means that the job was queued. The participant
        attribute is published by the worker after AMD enters its listening setup.
        ``gave_up`` is polled between attribute checks. When it returns True the
        worker has already finalized the call (for example a BYO key that does
        not work) and left, so waiting out ``timeout`` would only delay the
        answer: :class:`WorkerGaveUp` is raised instead.
        https://docs.livekit.io/reference/python/livekit/api/room_service.html
        """
        async with asyncio.timeout(timeout):
            while True:
                result = await self._lkapi.room.list_participants(
                    api.ListParticipantsRequest(room=room_name)
                )
                if any(
                    p.attributes.get(VOICE_READY_ATTRIBUTE) == "true"
                    for p in result.participants
                ):
                    return
                if gave_up is not None and await gave_up():
                    raise WorkerGaveUp(room_name)
                await asyncio.sleep(poll_interval)

    async def add_inbound_number(self, trunk_id: str, e164: str) -> None:
        """Register ``e164`` on an inbound trunk so LiveKit accepts calls to it.

        ``ListUpdate(add=...)`` is a set-add on the trunk's ``numbers``: a
        number already listed stays listed once.
        """
        await self._lkapi.sip.update_inbound_trunk_fields(
            trunk_id, numbers=ListUpdate(add=[e164])
        )

    async def remove_inbound_number(self, trunk_id: str, e164: str) -> None:
        """Drop ``e164`` from an inbound trunk; a number not listed is a no-op.

        Refuses a connection placeholder: an empty list makes the trunk a wildcard.
        """
        if e164.startswith(PBX_TRUNK_PREFIX):
            raise ValueError("a connection trunk placeholder cannot be removed")
        await self._lkapi.sip.update_inbound_trunk_fields(
            trunk_id, numbers=ListUpdate(remove=[e164])
        )

    async def create_pbx_inbound_trunk(
        self, connection_id: UUID, username: str, password: str
    ) -> str:
        """Create a connection's inbound trunk, listing only its placeholder."""
        placeholder = pbx_placeholder(connection_id)
        info = await self._lkapi.sip.create_inbound_trunk(
            api.CreateSIPInboundTrunkRequest(
                trunk=api.SIPInboundTrunkInfo(
                    name=placeholder,
                    numbers=[placeholder],
                    auth_username=username,
                    auth_password=password,
                )
            )
        )
        return info.sip_trunk_id

    async def set_pbx_inbound_credentials(
        self, trunk_id: str, username: str, password: str
    ) -> None:
        """Change only the auth fields; ``numbers`` is never sent."""
        await self._lkapi.sip.update_inbound_trunk_fields(
            trunk_id, auth_username=username, auth_password=password
        )

    async def create_pbx_outbound_trunk(
        self,
        connection_id: UUID,
        host: str,
        port: int,
        transport: str,
        username: str | None,
        password: str | None,
    ) -> str:
        """Create a connection's outbound trunk to the customer's phone system.

        ``numbers=["*"]`` lets a dial use any caller ID (``sip_number``).
        """
        info = await self._lkapi.sip.create_outbound_trunk(
            api.CreateSIPOutboundTrunkRequest(
                trunk=api.SIPOutboundTrunkInfo(
                    name=f"{pbx_placeholder(connection_id)}-out",
                    address=f"{host}:{port}",
                    transport=_PBX_TRANSPORTS[transport],
                    numbers=["*"],
                    auth_username=username or "",
                    auth_password=password or "",
                )
            )
        )
        return info.sip_trunk_id

    async def update_pbx_outbound_trunk(
        self,
        trunk_id: str,
        host: str,
        port: int,
        transport: str,
        username: str | None,
        password: str | None,
    ) -> None:
        """Point an outbound trunk at a new address, transport and auth."""
        await self._lkapi.sip.update_outbound_trunk_fields(
            trunk_id,
            address=f"{host}:{port}",
            transport=_PBX_TRANSPORTS[transport],
            auth_username=username or "",
            auth_password=password or "",
        )

    async def clear_dispatch_rule_trunks(
        self, rule_name: str
    ) -> tuple[str, list[str]] | None:
        """Make the named dispatch rule match any inbound trunk (empty
        ``trunk_ids``). Returns the rule id and its trunk list, or ``None``
        when no rule has that name."""
        listed = await self._lkapi.sip.list_dispatch_rule(
            api.ListSIPDispatchRuleRequest()
        )
        info = next((r for r in listed.items if r.name == rule_name), None)
        if info is None:
            return None
        info.ClearField("trunk_ids")
        updated = await self._lkapi.sip.update_dispatch_rule(
            info.sip_dispatch_rule_id, info
        )
        return updated.sip_dispatch_rule_id, list(updated.trunk_ids)

    async def delete_sip_trunk(self, trunk_id: str) -> None:
        """Delete a trunk; a trunk that is already gone (404) counts as done."""
        try:
            await self._lkapi.sip.delete_trunk(
                api.DeleteSIPTrunkRequest(sip_trunk_id=trunk_id)
            )
        except api.TwirpError as exc:
            if exc.status != 404:
                raise
