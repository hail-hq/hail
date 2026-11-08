"""Native SIP backstops, measured from before dialing rather than answer.

LiveKit cannot extend an existing leg's timer on handover. Reserve the
possible handover on the caller leg; the worker still enforces each phase.
https://docs.livekit.io/reference/telephony/sip-api/#createsipparticipant
"""

from math import ceil

# LiveKit's documented upper bound for outbound ringing, without changing
# the trunk's current ring window. Native duration includes this time.
SIP_RINGING_MAX_SECONDS = 80
# Covers the bounded goodbye (10s) and teardown (up to 8s), with slack.
SIP_HANGUP_GRACE_SECONDS = 30

HANDOVER_RING_SECONDS = 30
# A silent pickup is checked before the contact is connected.
HANDOVER_SILENCE_SECONDS = 15.0
# An immediate hangup after speech must not count as a successful handover.
HANDOVER_SETTLE_SECONDS = 10.0


def outbound_call_backstop(
    agent_seconds: int | None, handover_seconds: int = 0
) -> int | None:
    if agent_seconds is None or agent_seconds <= 0:
        # A transfer may start arbitrarily late when the agent cap is off.
        return None
    seconds = agent_seconds + SIP_RINGING_MAX_SECONDS + SIP_HANGUP_GRACE_SECONDS
    if handover_seconds > 0:
        seconds += handover_seconds + HANDOVER_SILENCE_SECONDS + HANDOVER_SETTLE_SECONDS
    return ceil(seconds)


def handover_call_backstop(handover_seconds: int) -> int | None:
    if handover_seconds <= 0:
        return None
    return ceil(
        handover_seconds
        + HANDOVER_RING_SECONDS
        + HANDOVER_SILENCE_SECONDS
        + HANDOVER_SETTLE_SECONDS
        + SIP_HANGUP_GRACE_SECONDS
    )
