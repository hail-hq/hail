"""Route an owned number through its carrier, never a cheapest foreign trunk.

Each carrier has its own LiveKit outbound SIP trunk. A number never leaves
its carrier: a DIDWW number must not dial through the Twilio trunk (Twilio
would reject or rewrite the caller ID) and vice versa. Adding a carrier is one
``Carrier`` entry in ``CARRIERS`` plus its provider adapters.
"""

from collections.abc import Callable
from dataclasses import dataclass

from hailhq.core.config import settings
from hailhq.core.providers.sms.base import SmsProvider
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider

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


def _twilio_sms(twilio: SmsProvider) -> SmsProvider:
    return twilio


def _telnyx_sms(twilio: SmsProvider) -> SmsProvider:
    if not settings.telnyx_public_key:
        raise ValueError("Telnyx webhooks are not configured")
    return TelnyxSmsProvider()


@dataclass(frozen=True)
class Carrier:
    voice_route: Callable[[], VoiceRoute]
    # None when Hail sends no SMS through this carrier.
    sms_route: Callable[[SmsProvider], SmsProvider] | None
    # Path under the API URL that receives this carrier's message status.
    sms_status_path: str | None
    # True when a purchase is accepted first and completes later.
    async_orders: bool


CARRIERS: dict[str, Carrier] = {
    TWILIO: Carrier(_twilio_voice, _twilio_sms, "sms/status", async_orders=False),
    TELNYX: Carrier(_telnyx_voice, _telnyx_sms, "sms/telnyx", async_orders=True),
    # Outbound voice only. Numbers are registered by hand:
    # docs/public/self-host/didww.md.
    DIDWW: Carrier(_didww_voice, None, None, async_orders=False),
}


def carrier(provider: str) -> Carrier:
    try:
        return CARRIERS[provider]
    except KeyError:
        raise ValueError(f"Unsupported number carrier: {provider!r}") from None


def voice_route(provider: str) -> VoiceRoute:
    return carrier(provider).voice_route()


def sms_route(provider: str, twilio: SmsProvider) -> SmsProvider:
    if provider not in CARRIERS:
        raise ValueError("Unsupported SMS carrier")
    route = CARRIERS[provider].sms_route
    if route is None:
        raise ValueError(f"{provider} numbers cannot send SMS through Hail")
    return route(twilio)


def sms_status_path(provider: str) -> str:
    path = carrier(provider).sms_status_path
    if path is None:
        raise ValueError(f"{provider} numbers cannot send SMS through Hail")
    return path
