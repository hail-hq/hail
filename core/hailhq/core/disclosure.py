"""The spoken AI line.

Spoken by the voicebot as the first thing on every call unless the agent or
the request opted out. The text is a template with one placeholder,
``{org}``, the organization's display name. Precedence, resolved by the
caller: the agent's line, else the workspace line
(``organization_call_settings.ai_disclosure_line``), else the built-in line
for the call's direction. Templates come from agents (``/agents``) and the
workspace call settings, never from the ``POST /calls`` body.
"""

from __future__ import annotations

from typing import Literal

Direction = Literal["outbound", "inbound"]

DEFAULT_OUTBOUND_LINE = "Hi, this is an AI assistant calling on behalf of {org}."
DEFAULT_INBOUND_LINE = "Hi, this is an AI assistant answering on behalf of {org}."

# The voicebot's TTS filter (``SpeechSanitizingAgent``) drops any turn that
# opens with tool-call syntax. A line that opens with one of these would be
# dropped whole and the disclosure never spoken.
_UNSPEAKABLE_OPENERS = ("{", "[", "`")

# What ``{org}`` becomes when the organization name did not resolve.
_UNNAMED = {
    "outbound": "whoever requested this call",
    "inbound": "this number",
}

__all__ = [
    "DEFAULT_INBOUND_LINE",
    "DEFAULT_OUTBOUND_LINE",
    "Direction",
    "disclosure_text",
]


def disclosure_text(
    direction: Direction, org_name: str | None, template: str | None = None
) -> str:
    """The exact line to speak. A blank template means "use the default", and
    so does one whose spoken text would open with tool-call syntax (``[``,
    ``{`` or a backtick): the voicebot would drop it, and the call would
    open with no AI line at all."""
    default = DEFAULT_INBOUND_LINE if direction == "inbound" else DEFAULT_OUTBOUND_LINE
    name = (org_name or "").strip() or _UNNAMED[direction]
    line = (template or "").strip() or default
    spoken = line.replace("{org}", name)
    if spoken.lstrip().startswith(_UNSPEAKABLE_OPENERS):
        spoken = default.replace("{org}", name)
    return spoken
