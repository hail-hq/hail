"""Register a number for inbound calls, at the carrier and at LiveKit.

Two steps, in this order: the carrier points the number at LiveKit
(``Carrier.attach_inbound``), then LiveKit's inbound trunk for that carrier
lists the number so the INVITE is accepted. ``phone_numbers.inbound_registered_at``
is set only when both succeeded. ``unregister`` is the reverse (carrier
detach first, then LiveKit), run when the number stops routing calls to an
agent and before a release.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hailhq.core.carrier_routing import carrier
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import PhoneNumber
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

__all__ = ["InboundRoutingError", "register", "unregister"]


class InboundRoutingError(Exception):
    """A step failed. ``stage`` is ``carrier_attach``, ``carrier_detach``,
    ``livekit_add`` or ``livekit_remove``; ``config`` is True when the
    operator has not configured that carrier for inbound."""

    def __init__(self, stage: str, exc: Exception, *, config: bool = False) -> None:
        super().__init__(f"{stage}: {exc}")
        self.stage = stage
        self.config = config
        self.__cause__ = exc


async def register(db: AsyncSession, lk: LiveKitClient, number: PhoneNumber) -> None:
    """Idempotent: a registered number returns at once."""
    if number.inbound_registered_at is not None:
        return
    entry = carrier(number.provider)
    try:
        trunk_id = entry.inbound_trunk()
    except ValueError as exc:
        raise InboundRoutingError("livekit_add", exc, config=True) from exc
    try:
        await entry.attach_inbound(number.provider_resource_id, number.e164)
    except Exception as exc:
        raise InboundRoutingError(
            "carrier_attach", exc, config=_is_config_error(exc)
        ) from exc
    try:
        await lk.add_inbound_number(trunk_id, number.e164)
    except Exception as exc:
        # Do not leave the carrier pointing at a LiveKit trunk that refuses
        # the number: callers would hear a failure tone until someone retries.
        try:
            await entry.detach_inbound(number.provider_resource_id, number.e164)
        except Exception:
            logger.warning(
                "number %s: carrier detach after LiveKit failure also failed",
                number.e164,
                exc_info=True,
            )
        raise InboundRoutingError("livekit_add", exc) from exc
    number.inbound_registered_at = datetime.now(timezone.utc)
    await db.flush()


async def unregister(db: AsyncSession, lk: LiveKitClient, number: PhoneNumber) -> None:
    """Idempotent: an unregistered number returns at once."""
    if number.inbound_registered_at is None:
        return
    entry = carrier(number.provider)
    try:
        trunk_id = entry.inbound_trunk()
    except ValueError as exc:
        raise InboundRoutingError("livekit_remove", exc, config=True) from exc
    # Carrier first: while the carrier still sends calls, the LiveKit trunk
    # must keep accepting them. A failed detach leaves LiveKit and the row
    # untouched, so a retry starts from the same state.
    try:
        await entry.detach_inbound(number.provider_resource_id, number.e164)
    except Exception as exc:
        raise InboundRoutingError(
            "carrier_detach", exc, config=_is_config_error(exc)
        ) from exc
    try:
        await lk.remove_inbound_number(trunk_id, number.e164)
    except Exception as exc:
        # The carrier is already detached; the row stays registered so the
        # next unregister (idempotent at the carrier) finishes the removal.
        raise InboundRoutingError("livekit_remove", exc) from exc
    number.inbound_registered_at = None
    await db.flush()


def _is_config_error(exc: Exception) -> bool:
    from hailhq.core.providers.voice.base import CarrierNotConfigured

    return isinstance(exc, CarrierNotConfigured)
