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

from hailhq.core.carrier_routing import PBX, carrier, inbound_trunk
from hailhq.core.config import settings
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import PhoneNumber
from livekit.api import TwirpError
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
    try:
        trunk_id = await inbound_trunk(db, number)
    except ValueError as exc:
        raise InboundRoutingError("livekit_add", exc, config=True) from exc
    # A connected number has no carrier: the customer's phone system sends
    # the call straight to Hail's trunk for that connection.
    entry = None if number.provider == PBX else carrier(number.provider)
    if entry is not None:
        try:
            await entry.attach_inbound(number.provider_resource_id, number.e164)
        except Exception as exc:
            raise InboundRoutingError(
                "carrier_attach", exc, config=_is_config_error(exc)
            ) from exc
    try:
        # A stale entry on the shared trunk would make LiveKit refuse the add
        # (a number is listed on one trunk only). Best effort: a shared-trunk
        # problem must not stop the add; a refused add is handled below.
        try:
            await _remove_from_shared(lk, trunk_id, number.e164)
        except Exception:
            logger.warning(
                "number %s: could not clear it from the shared inbound trunk",
                number.e164,
                exc_info=True,
            )
        await lk.add_inbound_number(trunk_id, number.e164)
    except Exception as exc:
        # Do not leave the carrier pointing at a LiveKit trunk that refuses
        # the number: callers would hear a failure tone until someone retries.
        if entry is not None:
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
    if number.provider == PBX:
        # The number stays on its connection trunk while the row is
        # ``unverified`` or ``active``: a call then reaches Hail and gets a
        # ``no_agent`` refusal the customer can see. The trunk never goes
        # empty (an empty list would accept any number).
        number.inbound_registered_at = None
        await db.flush()
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
    try:
        # While numbers move, a number may still be on the shared trunk.
        # Left there, a later register to its carrier's trunk is refused.
        await _remove_from_shared(lk, trunk_id, number.e164)
    except Exception as exc:
        # The row stays registered, so the next unregister retries.
        raise InboundRoutingError("livekit_remove", exc) from exc
    number.inbound_registered_at = None
    await db.flush()


async def _remove_from_shared(lk: LiveKitClient, trunk_id: str, e164: str) -> None:
    """Drop ``e164`` from the shared trunk when its carrier has its own trunk.
    A shared trunk already deleted (retired) holds nothing: that is done."""
    shared = settings.livekit_sip_inbound_trunk_id
    if not shared or shared == trunk_id:
        return
    try:
        await lk.remove_inbound_number(shared, e164)
    except TwirpError as exc:
        if exc.code != "not_found":
            raise


def _is_config_error(exc: Exception) -> bool:
    from hailhq.core.providers.voice.base import CarrierNotConfigured

    return isinstance(exc, CarrierNotConfigured)
