"""Carrier-side voice provider interface.

A ``VoiceProvider`` covers the **carrier** concerns of an outbound call:
provisioning / releasing E.164 numbers and inspecting / hanging up an
in-flight call by its provider-native call id (e.g. a Twilio Call SID).

Outbound dial itself is **not** here. In v1 the actual SIP outbound is
performed by LiveKit's SIP service (see ``core/hailhq/core/livekit.py``,
Task 4 of the v1 plan), with Twilio as the underlying trunk. Splitting
the abstractions this way keeps the carrier swap-out story honest:
swapping Twilio for another DID provider only touches this interface;
swapping LiveKit for another media server only touches the LiveKit
helpers.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from pydantic import BaseModel

from hailhq.core.schemas import NumberType

__all__ = [
    "NumberType",
    "ProviderCallStatus",
    "ProviderNumber",
    "VoiceProvider",
]


class ProviderNumber(BaseModel):
    """A phone number resource as returned by the carrier."""

    provider_resource_id: str
    e164: str
    country_code: str
    capabilities: list[str]
    number_type: NumberType


class ProviderCallStatus(BaseModel):
    """A snapshot of a single in-flight or finished call at the carrier.

    ``status`` is the provider-native string (e.g. Twilio's
    ``"in-progress"``, ``"completed"``, ``"no-answer"``). The mapping to
    Hail's own ``CallStatus`` enum lives in the API layer so the
    provider adapter stays a thin shim.
    """

    provider_call_sid: str
    status: str
    answered_at: datetime | None = None
    ended_at: datetime | None = None
    duration_seconds: int | None = None


class VoiceProvider(ABC):
    """Abstract carrier-side voice provider."""

    @abstractmethod
    async def acquire_number(
        self,
        country_code: str,
        number_type: NumberType,
        capabilities: list[str],
    ) -> ProviderNumber:
        """Search for and purchase a number matching the criteria."""

    @abstractmethod
    async def release_number(self, provider_resource_id: str) -> None:
        """Release a previously-acquired number back to the carrier."""

    @abstractmethod
    async def get_call_status(self, provider_call_sid: str) -> ProviderCallStatus:
        """Fetch the carrier's view of a single call."""

    @abstractmethod
    async def hangup_call(self, provider_call_sid: str) -> None:
        """Ask the carrier to terminate an in-flight call."""
