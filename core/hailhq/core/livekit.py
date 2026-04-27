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

import json
from uuid import UUID

from livekit import api

from hailhq.core.config import settings


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
        self._lkapi = api.LiveKitAPI(
            url or settings.livekit_url,
            api_key or settings.livekit_api_key,
            api_secret or settings.livekit_api_secret,
        )

    async def aclose(self) -> None:
        """Close the underlying aiohttp session.

        Safe to call multiple times; ``LiveKitAPI.aclose`` is idempotent.
        """
        await self._lkapi.aclose()

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
        result = await self._lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                room=room_name,
                agent_name=agent_name,
                metadata=json.dumps(metadata),
            )
        )
        return result.id

    async def create_sip_participant(
        self,
        room_name: str,
        to_e164: str,
        from_e164: str,
        sip_trunk_id: str,
        participant_identity: str,
    ) -> api.SIPParticipantInfo:
        """Place an outbound SIP call into ``room_name`` via LiveKit.

        Per ``livekit_sip.proto``:

        * ``sip_call_to`` is the **destination** (the number being dialed).
        * ``sip_number`` is the **caller-ID / from** number presented to
          the callee. When empty, the trunk's configured number is used.

        LiveKit's SIP service issues the INVITE through the trunk pointed to
        by ``sip_trunk_id`` (which in v1 is a Twilio Elastic SIP Trunk).
        """
        return await self._lkapi.sip.create_sip_participant(
            api.CreateSIPParticipantRequest(
                sip_trunk_id=sip_trunk_id,
                sip_call_to=to_e164,
                sip_number=from_e164,
                room_name=room_name,
                participant_identity=participant_identity,
                participant_name=participant_identity,
            )
        )
