"""Route an owned number through its carrier, never a cheapest foreign trunk.

Each carrier has its own LiveKit outbound SIP trunk. A number never leaves
its carrier: a DIDWW number must not dial through the Twilio trunk (Twilio
would reject or rewrite the caller ID) and vice versa.

``CARRIERS`` is the one place that names the carriers. Quotes, orders and
release reach a carrier only through its ``Carrier`` entry. Adding a carrier
is one adapter module in ``providers/voice/`` with the interface functions
(``offers``, ``place_order``, ``order_outcome``, ``release``, ``leg_costs``
and, when it registers end users after the purchase, ``revoke``) plus one
entry here.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal
from uuid import UUID

import httpx
import requests
from didww.exceptions import DidwwApiError, DidwwClientError
from hailhq.core.carrier_offer import Approval, CarrierOffer
from hailhq.core.config import settings
from hailhq.core.models import PhoneNumber
from hailhq.core.providers.sms import twilio as twilio_sms
from hailhq.core.providers.sms.base import SmsProvider
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
from hailhq.core.providers.voice import OrderState, didww, telnyx, twilio
from hailhq.core.providers.voice.base import (
    CarrierNotConfigured,
    CarrierRequestError,
    LegCosts,
)
from hailhq.core.schemas import NumberType
from hailhq.core.sip_connections import connection_of
from sqlalchemy.ext.asyncio import AsyncSession
from twilio.base.exceptions import TwilioRestException

TWILIO = "twilio"
TELNYX = "telnyx"
DIDWW = "didww"
PBX = "pbx"

# LiveKit outbound SIP trunk id plus the SIP headers that trunk needs.
VoiceRoute = tuple[str, dict[str, str]]


def _trunk(provider: str, setting: str) -> str:
    """The trunk id stored under ``setting``; a ``ValueError`` when empty so
    the caller fails before creating any LiveKit resources."""
    trunk_id = getattr(settings, setting)
    if not trunk_id:
        raise ValueError(
            f"{provider} SIP routing is not configured ({setting.upper()})"
        )
    return trunk_id


def _twilio_voice() -> VoiceRoute:
    return _trunk(TWILIO, "livekit_twilio_sip_outbound_trunk_id"), {}


def _telnyx_voice() -> VoiceRoute:
    trunk_id = _trunk(TELNYX, "livekit_telnyx_sip_outbound_trunk_id")
    if not settings.telnyx_sip_username:
        raise ValueError(
            f"{TELNYX} SIP routing is not configured (TELNYX_SIP_USERNAME)"
        )
    return trunk_id, {"X-Telnyx-Username": settings.telnyx_sip_username}


def _didww_voice() -> VoiceRoute:
    return _trunk(DIDWW, "livekit_didww_sip_outbound_trunk_id"), {}


def _inbound(provider: str, setting: str) -> str:
    """The inbound trunk for ``provider``: its override, else the shared
    ``LIVEKIT_SIP_INBOUND_TRUNK_ID`` (config.py folds the two together)."""
    trunk_id = getattr(settings, setting)
    if not trunk_id:
        raise ValueError(
            f"{provider} inbound is not configured "
            f"({setting.upper()} or LIVEKIT_SIP_INBOUND_TRUNK_ID)"
        )
    return trunk_id


def _twilio_inbound() -> str:
    return _inbound(TWILIO, "livekit_twilio_sip_inbound_trunk_id")


def _telnyx_inbound() -> str:
    return _inbound(TELNYX, "livekit_telnyx_sip_inbound_trunk_id")


def _didww_inbound() -> str:
    return _inbound(DIDWW, "livekit_didww_sip_inbound_trunk_id")


def _twilio_sms() -> SmsProvider:
    return twilio_sms.twilio_sms_provider()


def sms_route_configured(provider: str) -> bool:
    """Credentials and callback verification needed by the SMS adapters."""
    if provider == TWILIO:
        return bool(settings.twilio_account_sid and settings.twilio_auth_token)
    if provider == TELNYX:
        return bool(settings.telnyx_api_key and settings.telnyx_public_key)
    return False


def _telnyx_sms() -> SmsProvider:
    if not sms_route_configured(TELNYX):
        raise ValueError("Telnyx SMS credentials or webhooks are not configured")
    return TelnyxSmsProvider()


# Carrier-side inbound work for one owned number: (provider_resource_id, e164).
InboundHook = Callable[[str | None, str], Awaitable[None]]

# (state, owned resource id, carrier order id).
Outcome = tuple[OrderState, str | None, str | None]


@dataclass(frozen=True)
class Carrier:
    voice_route: Callable[[], VoiceRoute]
    # This carrier's SMS client. None when Hail sends no SMS through it.
    sms_route: Callable[[], SmsProvider] | None
    # Path under the API URL that receives this carrier's message status.
    sms_status_path: str | None
    # True when a purchase is accepted first and completes later.
    async_orders: bool
    # Live inventory: (org, country, number type, capabilities, exact e164,
    # national digits the number starts with, city, US/CA state code, the
    # org's approved Hail verifications for this carrier, newest first).
    # The carrier only confirms each approval is still valid; it never finds
    # one Hail has no row for.
    offers: Callable[
        [
            UUID,
            str,
            NumberType,
            list[str],
            str | None,
            str | None,
            str | None,
            str | None,
            list[Approval],
        ],
        Awaitable[list[CarrierOffer]],
    ]
    # Buy one quoted number once, never retried: (number id, offer). Returns
    # the carrier order id when ``async_orders``, else the owned resource id.
    place_order: Callable[[UUID, CarrierOffer], Awaitable[str]]
    # What happened to an order: (e164, number id, order id if known, offer).
    order_outcome: Callable[[str, UUID, str | None, CarrierOffer], Awaitable[Outcome]]
    # Give an owned number back: (resource id). ``CarrierNotConfigured`` when
    # the carrier's credentials are missing.
    release: Callable[[str], Awaitable[None]]
    # LiveKit inbound trunk this carrier's numbers are registered on.
    inbound_trunk: Callable[[], str]
    # Point the number at LiveKit at the carrier, and undo it:
    # (provider_resource_id, e164).
    attach_inbound: InboundHook
    detach_inbound: InboundHook
    # What the carrier charged for each call leg, from its call records.
    leg_costs: LegCosts
    # How long a pending order may wait for the carrier before Hail fails
    # it and refunds the hold. DIDWW registers the end user after the
    # purchase, which takes days; the others answer within minutes.
    pending_timeout: timedelta = timedelta(hours=2)
    # How long the reconciler waits between two checks of one pending
    # order. DIDWW answers "pending" for days while a person reviews the
    # registration, so it is asked far less often.
    poll_interval: timedelta = timedelta(seconds=15)
    # Take back an approved end-user registration the carrier rejected after
    # the purchase: (offer, org). Returns the carrier's reason, if any. None
    # for a carrier that never rejects after the purchase.
    revoke_registration: (
        Callable[[CarrierOffer, UUID], Awaitable[str | None]] | None
    ) = None
    # True when Hail can file a US/CA toll-free texting verification with
    # this carrier. Without it, a toll-free number there can never text.
    toll_free_sms_verification: bool = False


# Listed in offer order: the first carrier wins a tie on price.
CARRIERS: dict[str, Carrier] = {
    TWILIO: Carrier(
        _twilio_voice,
        _twilio_sms,
        "sms/status",
        async_orders=False,
        offers=twilio.offers,
        place_order=twilio.place_order,
        order_outcome=twilio.order_outcome,
        release=twilio.release,
        inbound_trunk=_twilio_inbound,
        attach_inbound=twilio.attach_inbound_number,
        detach_inbound=twilio.detach_inbound_number,
        leg_costs=twilio.leg_costs,
        toll_free_sms_verification=True,
    ),
    TELNYX: Carrier(
        _telnyx_voice,
        _telnyx_sms,
        "sms/telnyx",
        async_orders=True,
        offers=telnyx.offers,
        place_order=telnyx.place_order,
        order_outcome=telnyx.order_outcome,
        release=telnyx.release,
        inbound_trunk=_telnyx_inbound,
        attach_inbound=telnyx.attach_inbound_number,
        detach_inbound=telnyx.detach_inbound_number,
        leg_costs=telnyx.leg_costs,
    ),
    # Voice only (no SMS). Orders complete after DIDWW approves the
    # end-user registration: docs/public/self-host/didww.md.
    DIDWW: Carrier(
        _didww_voice,
        None,
        None,
        async_orders=True,
        offers=didww.offers,
        place_order=didww.place_order,
        order_outcome=didww.order_outcome,
        release=didww.release,
        inbound_trunk=_didww_inbound,
        attach_inbound=didww.attach_inbound_number,
        detach_inbound=didww.detach_inbound_number,
        leg_costs=didww.leg_costs,
        pending_timeout=timedelta(days=7),
        poll_interval=timedelta(minutes=15),
        revoke_registration=didww.revoke,
    ),
}


def carrier(provider: str) -> Carrier:
    try:
        return CARRIERS[provider]
    except KeyError:
        raise ValueError(f"Unsupported number carrier: {provider!r}") from None


async def voice_route(db: AsyncSession, number: PhoneNumber) -> VoiceRoute:
    """The LiveKit outbound trunk (and SIP headers) that dials from ``number``."""
    if number.provider != PBX:
        return carrier(number.provider).voice_route()
    conn = await connection_of(db, number)
    if conn.livekit_outbound_trunk_id is None:
        raise ValueError("this phone system has no address for calls out")
    return conn.livekit_outbound_trunk_id, {}


def sms_route(provider: str) -> SmsProvider:
    if provider not in CARRIERS:
        raise ValueError("Unsupported SMS carrier")
    route = CARRIERS[provider].sms_route
    if route is None:
        raise ValueError(f"{provider} numbers cannot send SMS through Hail")
    return route()


def sms_status_path(provider: str) -> str:
    path = carrier(provider).sms_status_path
    if path is None:
        raise ValueError(f"{provider} numbers cannot send SMS through Hail")
    return path


async def inbound_trunk(db: AsyncSession, number: PhoneNumber) -> str:
    """The LiveKit inbound trunk an INVITE for ``number`` must arrive on."""
    if number.provider != PBX:
        return carrier(number.provider).inbound_trunk()
    conn = await connection_of(db, number)
    if conn.livekit_inbound_trunk_id is None:
        raise ValueError("this phone system has no inbound trunk")
    return conn.livekit_inbound_trunk_id


# What a failed carrier request means for a delete (``failure_kind``):
#   gone     404, 410: the carrier no longer holds it; the step is done.
#   outage   5xx; 401 (Hail's own credentials); 408, 425, 429 (timing and
#            rate limits); a timeout, a lost connection, or the carrier not
#            configured here. Retried with no limit.
#   refused  403, 409: the carrier refuses this record for now (Telnyx keeps a
#            brand for 3 months after its campaign). Account close retries
#            it for a bounded time.
#   final    any other 4xx (400, 422...): a retry gets the same answer.
FailureKind = Literal["gone", "outage", "refused", "final"]
_GONE = frozenset({404, 410})
_NOT_NOW_4XX = frozenset({401, 408, 425, 429})
_REFUSED_4XX = frozenset({403, 409})
# No carrier answer, and a retry can work: the network, a timeout, a
# response cut off or garbled in transit, or configuration an operator
# fixes. The whole httpx and requests families: a ChunkedEncodingError or
# a DecodingError is a blip, not a refusal.
_NOT_NOW_ERRORS = (
    httpx.RequestError,
    requests.RequestException,
    TimeoutError,
    ConnectionError,
    CarrierNotConfigured,
    DidwwClientError,
)


def _chain(exc: BaseException):
    """``exc`` and the errors it was raised from, five at most. A
    ``raise ... from None`` ends the chain: the hidden context does not count."""
    seen: BaseException | None = exc
    for _ in range(5):
        if seen is None:
            return
        yield seen
        if seen.__cause__ is not None:
            seen = seen.__cause__
        elif seen.__suppress_context__:
            return
        else:
            seen = seen.__context__


def _http_status(exc: BaseException) -> int | None:
    if isinstance(exc, (CarrierRequestError, TwilioRestException)):
        return exc.status
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    if isinstance(exc, DidwwApiError):
        return exc.status_code
    return None


def carrier_http_status(exc: BaseException) -> int | None:
    """The HTTP status of the carrier answer behind ``exc``, else None (a
    timeout, a lost connection). Reads ``exc`` and the errors it was raised
    from, so a plug-in error that wraps the carrier's still counts."""
    for seen in _chain(exc):
        status = _http_status(seen)
        if status is not None:
            return status
    return None


def failure_kind(exc: BaseException, *, unknown: FailureKind = "outage") -> FailureKind:
    """How a failed carrier request counts (the table above). Reads ``exc``
    and the errors it was raised from. ``unknown`` is the kind of an error
    with no carrier answer that is not a network, timeout or configuration
    error: a ``discard`` step passes "refused" (a bug in Hail or the
    plug-in must not retry for good); elsewhere, such an error (a database
    error) is retried. Never reads the carrier's text.

    The error nearest ``exc`` that has a carrier status or is a network
    error decides: a lost connection while handling a 403 is an outage,
    and a plug-in error raised from a 409 is a refusal."""
    status = None
    for seen in _chain(exc):
        status = _http_status(seen)
        if status is not None:
            break
        if isinstance(seen, _NOT_NOW_ERRORS):
            return "outage"
    if status is None:
        return unknown
    if status in _GONE:
        return "gone"
    if not 400 <= status < 500 or status in _NOT_NOW_4XX:
        return "outage"
    if status in _REFUSED_4XX:
        return "refused"
    return "final"
