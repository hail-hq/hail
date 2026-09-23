"""Route an owned number through its carrier, never a cheapest foreign trunk."""

from hailhq.core.config import settings


def voice_route(provider: str) -> tuple[str, dict[str, str]]:
    if provider == "twilio":
        return settings.livekit_sip_outbound_trunk_id, {}
    if provider == "telnyx":
        if (
            not settings.livekit_telnyx_sip_outbound_trunk_id
            or not settings.telnyx_sip_username
        ):
            raise ValueError("Telnyx SIP routing is not configured")
        return settings.livekit_telnyx_sip_outbound_trunk_id, {
            "X-Telnyx-Username": settings.telnyx_sip_username,
        }
    raise ValueError("Unsupported number carrier")


def sms_route(provider: str, twilio):
    if provider == "twilio":
        return twilio
    if provider == "telnyx":
        from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider

        if not settings.telnyx_public_key:
            raise ValueError("Telnyx webhooks are not configured")
        return TelnyxSmsProvider()
    raise ValueError("Unsupported SMS carrier")
