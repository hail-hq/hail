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
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from hailhq.core.schemas import NumberType
from pydantic import BaseModel

__all__ = [
    "CarrierNotConfigured",
    "CarrierPreOrderError",
    "CarrierRequestError",
    "LegCost",
    "LegCosts",
    "LegRef",
    "NumberType",
    "OrderState",
    "ProviderCallStatus",
    "VoiceProvider",
]


# What a carrier says about a number order. ``missing``: the carrier has no
# record of it. ``rejected_registration``: the number was bought but the
# carrier refused the end-user registration.
OrderState = Literal["active", "failed", "pending", "missing", "rejected_registration"]


class CarrierRequestError(Exception):
    """A carrier rejected or failed a request. ``status`` is its HTTP status."""

    def __init__(self, status: int) -> None:
        super().__init__(f"Carrier request failed with HTTP {status}")
        self.status = status


class CarrierPreOrderError(CarrierRequestError):
    """The request failed before any order was sent to the carrier. Nothing
    was bought, so the caller can fail the purchase and refund at once."""


class CarrierNotConfigured(Exception):
    """The carrier is not configured on this deployment (an operator problem)."""


@dataclass(frozen=True)
class LegRef:
    """One call leg to look up in a carrier's call records."""

    id: UUID
    direction: Literal["inbound", "outbound"]
    sip_call_id: str | None
    carrier_call_id: str | None
    joined_at: datetime
    left_at: datetime | None


@dataclass(frozen=True)
class LegCost:
    """What the carrier charged for one leg, in USD."""

    usd: Decimal
    billed_seconds: int | None = None
    usd_per_minute: Decimal | None = None


# A carrier's cost lookup: the legs found in its records, by leg id. A leg
# left out has no record yet (or never will); the caller asks again later.
LegCosts = Callable[[list[LegRef]], Awaitable[dict[UUID, LegCost]]]


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

    # Registry name shared with this carrier's verification plug-in.
    carrier: str

    @abstractmethod
    async def release_number(self, provider_resource_id: str) -> None:
        """Release a previously-acquired number back to the carrier."""

    @abstractmethod
    async def get_call_status(self, provider_call_sid: str) -> ProviderCallStatus:
        """Fetch the carrier's view of a single call."""

    @abstractmethod
    async def hangup_call(self, provider_call_sid: str) -> None:
        """Ask the carrier to terminate an in-flight call."""
