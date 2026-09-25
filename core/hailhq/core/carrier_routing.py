"""Route an outbound call through the carrier that owns its from-number.

Each carrier has its own LiveKit outbound SIP trunk. A number never leaves
its carrier: a DIDWW number must not dial through the Twilio trunk (Twilio
would reject or rewrite the caller ID) and vice versa.
"""

from hailhq.core.config import settings


def voice_route(provider: str) -> str:
    """LiveKit outbound SIP trunk id for ``provider`` (``PhoneNumber.provider``)."""
    if provider == "twilio":
        return settings.livekit_sip_outbound_trunk_id
    if provider == "didww":
        if not settings.livekit_didww_sip_outbound_trunk_id:
            raise ValueError(
                "DIDWW SIP routing is not configured "
                "(LIVEKIT_DIDWW_SIP_OUTBOUND_TRUNK_ID)"
            )
        return settings.livekit_didww_sip_outbound_trunk_id
    raise ValueError(f"Unsupported number carrier: {provider!r}")
