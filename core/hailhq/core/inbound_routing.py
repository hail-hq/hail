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
from hailhq.core.config import settings
from hailhq.core.livekit import LiveKitClient
from hailhq.core.models import PhoneNumber
from livekit.api.twirp_client import TwirpError
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
        await _add_to_carrier_trunk(lk, trunk_id, number.e164)
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
    try:
        await _remove_from_shared_trunk(lk, trunk_id, number.e164)
    except Exception:
        # The carrier is detached and the carrier trunk cleared: the number
        # is unregistered and the carrier sends it no calls. The leftover
        # stays until the next register removes it, or until
        # sip_trunks.py move puts it on the carrier's locked trunk.
        logger.error(
            "number %s: still listed on the shared inbound trunk %s (removal "
            "failed). It takes no calls: the carrier is detached. Its next "
            "register removes it, or run scripts/ops/sip_trunks.py move "
            "--carrier %s --yes to put it on the carrier's locked trunk "
            "(docs/operations/inbound-trunk-lockdown.md)",
            number.e164,
            settings.livekit_sip_inbound_trunk_id,
            number.provider,
            exc_info=True,
        )
    number.inbound_registered_at = None
    await db.flush()


# livekit/protocol sip/sip.go, validateTrunkInbound.
_TRUNK_CONFLICT = "Conflicting inbound SIP Trunks"

# A SIP user name no call uses. A trunk with no numbers matches every dialed
# number, so the shared trunk keeps this entry before a number leaves it.
# Must equal ``shared_placeholder`` in infra/livekit/inbound-trunks.json.
SHARED_TRUNK_PLACEHOLDER = "hail-placeholder-shared"


async def _add_to_carrier_trunk(lk: LiveKitClient, trunk_id: str, e164: str) -> None:
    """Add, and once retry after a leftover shared-trunk entry is removed.

    Temporary, while numbers move to per-carrier trunks
    (docs/operations/inbound-trunk-lockdown.md; removal: issue #475). A
    number registered before its carrier got its own trunk can still be on
    the shared trunk, and LiveKit lists a number on one trunk only, so the
    add is refused. An unregistered number takes no calls, so removing the
    leftover loses nothing. The shared trunk is touched only after LiveKit
    refuses the add as a conflict (``invalid_argument``, "Conflicting inbound
    SIP Trunks" in livekit/protocol sip/sip.go)."""
    try:
        await lk.add_inbound_number(trunk_id, e164)
        return
    except TwirpError as first:
        shared = settings.livekit_sip_inbound_trunk_id
        if (
            first.code != "invalid_argument"
            or _TRUNK_CONFLICT not in first.message
            or not shared
            or shared == trunk_id
        ):
            raise
        logger.warning(
            "number %s: carrier trunk refused the add, removing a shared-trunk "
            "leftover and retrying",
            e164,
            exc_info=True,
        )
        try:
            await _remove_from_shared_trunk(lk, trunk_id, e164)
            await lk.add_inbound_number(trunk_id, e164)
        except Exception as exc:
            raise exc from first


async def _remove_from_shared_trunk(
    lk: LiveKitClient, trunk_id: str, e164: str
) -> None:
    """Drop ``e164`` from the shared trunk when a carrier has its own trunk.
    A deleted shared trunk (``not_found``) holds nothing: that is done."""
    shared = settings.livekit_sip_inbound_trunk_id
    if not shared or shared == trunk_id:
        return
    try:
        await lk.add_inbound_number(shared, SHARED_TRUNK_PLACEHOLDER)
        await lk.remove_inbound_number(shared, e164)
    except TwirpError as exc:
        if exc.code != "not_found":
            raise
        logger.warning("number %s: shared inbound trunk %s is gone", e164, shared)


def _is_config_error(exc: Exception) -> bool:
    from hailhq.core.providers.voice.base import CarrierNotConfigured

    return isinstance(exc, CarrierNotConfigured)
