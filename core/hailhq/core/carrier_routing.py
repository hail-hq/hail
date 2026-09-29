"""Route an owned number through its carrier, never a cheapest foreign trunk.

Each carrier has its own LiveKit outbound SIP trunk. A number never leaves
its carrier: a DIDWW number must not dial through the Twilio trunk (Twilio
would reject or rewrite the caller ID) and vice versa.

``CARRIERS`` is the one place that names the carriers. Quotes, orders and
release reach a carrier only through its ``Carrier`` entry. Adding a carrier
is one adapter module in ``providers/voice/`` with the five interface
functions (``offers``, ``place_order``, ``order_outcome``, ``release`` and,
when it registers end users after the purchase, ``revoke``) plus one entry
here.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from hailhq.core.carrier_offer import CarrierOffer
from hailhq.core.config import settings
from hailhq.core.providers.sms import twilio as twilio_sms
from hailhq.core.providers.sms.base import SmsProvider
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
from hailhq.core.providers.voice import OrderState, didww, telnyx, twilio
from hailhq.core.schemas import NumberType

TWILIO = "twilio"
TELNYX = "telnyx"
DIDWW = "didww"

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


def _twilio_sms() -> SmsProvider:
    return twilio_sms.twilio_sms_provider()


def _telnyx_sms() -> SmsProvider:
    if not settings.telnyx_public_key:
        raise ValueError("Telnyx webhooks are not configured")
    return TelnyxSmsProvider()


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
    # Live inventory: (org, country, number type, capabilities, exact e164).
    offers: Callable[
        [UUID, str, NumberType, list[str], str | None], Awaitable[list[CarrierOffer]]
    ]
    # Buy one quoted number once, never retried: (number id, offer). Returns
    # the carrier order id when ``async_orders``, else the owned resource id.
    place_order: Callable[[UUID, CarrierOffer], Awaitable[str]]
    # What happened to an order: (e164, number id, order id if known, offer).
    order_outcome: Callable[[str, UUID, str | None, CarrierOffer], Awaitable[Outcome]]
    # Give an owned number back: (resource id). ``CarrierNotConfigured`` when
    # the carrier's credentials are missing.
    release: Callable[[str], Awaitable[None]]
    # How long a pending order may wait for the carrier before Hail fails
    # it and refunds the hold. DIDWW registers the end user after the
    # purchase, which takes days; the others answer within minutes.
    pending_timeout: timedelta = timedelta(hours=2)
    # Take back an approved end-user registration the carrier rejected after
    # the purchase: (offer, org). Returns the carrier's reason, if any. None
    # for a carrier that never rejects after the purchase.
    revoke_registration: (
        Callable[[CarrierOffer, UUID], Awaitable[str | None]] | None
    ) = None


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
    ),
    # Outbound voice only. Orders complete after DIDWW approves the
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
        pending_timeout=timedelta(days=7),
        revoke_registration=didww.revoke,
    ),
}


def carrier(provider: str) -> Carrier:
    try:
        return CARRIERS[provider]
    except KeyError:
        raise ValueError(f"Unsupported number carrier: {provider!r}") from None


def voice_route(provider: str) -> VoiceRoute:
    return carrier(provider).voice_route()


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
