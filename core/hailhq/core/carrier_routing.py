"""Route an outbound call through the carrier that owns its from-number.

Each carrier has its own LiveKit outbound SIP trunk. A number never leaves
its carrier: a DIDWW number must not dial through the Twilio trunk (Twilio
would reject or rewrite the caller ID) and vice versa.
"""

from hailhq.core.config import settings

_TRUNK_SETTING = {
    "twilio": "livekit_sip_outbound_trunk_id",
    "didww": "livekit_didww_sip_outbound_trunk_id",
}


def voice_route(provider: str) -> str:
    """LiveKit outbound SIP trunk id for ``provider`` (``PhoneNumber.provider``).

    Raises ``ValueError`` for an unknown carrier or one whose trunk setting is
    empty, so the caller fails before creating any LiveKit resources.
    """
    setting = _TRUNK_SETTING.get(provider)
    if setting is None:
        raise ValueError(f"Unsupported number carrier: {provider!r}")
    trunk_id = getattr(settings, setting)
    if not trunk_id:
        raise ValueError(
            f"{provider} SIP routing is not configured ({setting.upper()})"
        )
    return trunk_id
