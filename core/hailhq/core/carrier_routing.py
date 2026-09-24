"""Route an owned number through its carrier, never a cheapest foreign trunk."""

from collections.abc import Callable
from dataclasses import dataclass

from hailhq.core.config import settings
from hailhq.core.providers.sms.base import SmsProvider
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider

TWILIO = "twilio"
TELNYX = "telnyx"


def _twilio_voice() -> tuple[str, dict[str, str]]:
    return settings.livekit_twilio_sip_outbound_trunk_id, {}


def _telnyx_voice() -> tuple[str, dict[str, str]]:
    if (
        not settings.livekit_telnyx_sip_outbound_trunk_id
        or not settings.telnyx_sip_username
    ):
        raise ValueError("Telnyx SIP routing is not configured")
    return settings.livekit_telnyx_sip_outbound_trunk_id, {
        "X-Telnyx-Username": settings.telnyx_sip_username,
    }


def _twilio_sms(twilio: SmsProvider) -> SmsProvider:
    return twilio


def _telnyx_sms(twilio: SmsProvider) -> SmsProvider:
    if not settings.telnyx_public_key:
        raise ValueError("Telnyx webhooks are not configured")
    return TelnyxSmsProvider()


@dataclass(frozen=True)
class Carrier:
    voice_route: Callable[[], tuple[str, dict[str, str]]]
    sms_route: Callable[[SmsProvider], SmsProvider]
    # Path under the API URL that receives this carrier's message status.
    sms_status_path: str
    # True when a purchase is accepted first and completes later.
    async_orders: bool


CARRIERS: dict[str, Carrier] = {
    TWILIO: Carrier(_twilio_voice, _twilio_sms, "sms/status", async_orders=False),
    TELNYX: Carrier(_telnyx_voice, _telnyx_sms, "sms/telnyx", async_orders=True),
}


def carrier(provider: str) -> Carrier:
    try:
        return CARRIERS[provider]
    except KeyError:
        raise ValueError("Unsupported number carrier") from None


def voice_route(provider: str) -> tuple[str, dict[str, str]]:
    return carrier(provider).voice_route()


def sms_route(provider: str, twilio: SmsProvider) -> SmsProvider:
    if provider not in CARRIERS:
        raise ValueError("Unsupported SMS carrier")
    return CARRIERS[provider].sms_route(twilio)
