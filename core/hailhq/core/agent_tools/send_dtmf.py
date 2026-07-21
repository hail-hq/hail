"""send_dtmf — press keypad digits on the live call.

session_control tier: purely local (the voicebot's ``send_dtmf`` handle),
no API call, affects only its own call. The voicebot wrapper waits for the
agent's pre-tool speech to finish playing so tones never go out over TTS.

LiveKit's own ``send_dtmf_events`` tool only exists after AMD returns a
``machine-ivr`` verdict (``AMD._run`` force-disables session-level
``ivr_detection`` and owns the IVR lifecycle), so a phone tree reached
mid-call would otherwise be unpressable. This tool is always available and
inherits the registry's allowlist, availability gate, failure wrapper, and
``tool_call`` event logging.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.agent_tools.spec import ToolContext, ToolSpec

# RFC 4733 named telephone events: digits 0-9 map to their own value, then
# `*`, `#`, and the rarely-used A-D tones.
DTMF_CODES: dict[str, int] = {
    **{str(d): d for d in range(10)},
    "*": 10,
    "#": 11,
    "A": 12,
    "B": 13,
    "C": 14,
    "D": 15,
}

# Matches LiveKit's DEFAULT_DTMF_PUBLISH_DELAY.
DIGIT_DELAY_SECONDS = 0.3

MAX_DIGITS = 32


def normalize_digits(raw: Any) -> str | None:
    """Uppercase + validate ``raw``; ``None`` when it is not a sendable string."""
    if not isinstance(raw, str):
        return None
    digits = raw.strip().upper()
    if not digits or len(digits) > MAX_DIGITS:
        return None
    if any(ch not in DTMF_CODES for ch in digits):
        return None
    return digits


async def _always(_org: uuid.UUID, _session: AsyncSession) -> bool:
    return True


async def _execute(ctx: ToolContext, args: dict[str, Any]) -> str:
    if ctx.send_dtmf is None:
        return "I can't press any keys on this call right now."
    digits = normalize_digits(args.get("digits"))
    if digits is None:
        return (
            "I can only press the digits zero through nine, star, pound, or "
            f"the letters A through D, up to {MAX_DIGITS} at a time."
        )
    for index, digit in enumerate(digits):
        if index:
            await asyncio.sleep(DIGIT_DELAY_SECONDS)
        await ctx.send_dtmf(digit)
    return "Done, I pressed those keys."


SPEC = ToolSpec(
    name="send_dtmf",
    description=(
        "Press keypad digits on this phone call — use it to answer an "
        "automated menu, enter an extension, or type a code. Pass the digits "
        "in the order they should be pressed, e.g. '1' or '4321#'. Allowed "
        "characters: 0-9, *, #, and the rarely-needed A-D tones."
    ),
    parameters={
        "type": "object",
        "properties": {
            "digits": {
                "type": "string",
                "description": "Digits to press in order, e.g. '123#'.",
            }
        },
        "required": ["digits"],
    },
    risk_tier="session_control",
    is_available=_always,
    execute=_execute,
)

__all__ = [
    "DIGIT_DELAY_SECONDS",
    "DTMF_CODES",
    "MAX_DIGITS",
    "SPEC",
    "normalize_digits",
]
