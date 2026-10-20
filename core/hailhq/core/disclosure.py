"""The spoken AI line.

Spoken by the voicebot as the first thing on every call unless the agent or
the request opted out. The text is a template with one placeholder,
``{org}``, the organization's display name. Precedence, resolved by the
caller: the agent's line, else the workspace line
(``organization_call_settings.ai_disclosure_line``), else the built-in line
for the call's direction. Templates come from agents (``/agents``) and the
workspace call settings, never from the ``POST /calls`` body.

An inbound agent with a first message and no custom line speaks the short
:data:`AFTER_GREETING_LINE` inside its own greeting instead (see
:func:`after_greeting_text`), so the caller hears one greeting, not two.
"""

from __future__ import annotations

from typing import Literal

Direction = Literal["outbound", "inbound"]

DEFAULT_OUTBOUND_LINE = "Hi, this is an AI assistant calling on behalf of {org}."
DEFAULT_INBOUND_LINE = "Hi, this is an AI assistant answering on behalf of {org}."
AFTER_GREETING_LINE = "I'm an AI assistant for {org}."

# The built-in lines when the organization name did not resolve. "On behalf
# of this number" told the caller nothing.
_UNNAMED_DEFAULT_LINE = "Hi, this is an AI assistant."
_UNNAMED_AFTER_GREETING_LINE = "I'm an AI assistant."

# The voicebot's TTS filter (``SpeechSanitizingAgent``) drops any turn that
# opens with tool-call syntax. A line that opens with one of these would be
# dropped whole and the disclosure never spoken.
_UNSPEAKABLE_OPENERS = ("{", "[", "`")

# What ``{org}`` becomes in a custom line when the organization name did not
# resolve.
_UNNAMED = {
    "outbound": "whoever requested this call",
    "inbound": "this number",
}

__all__ = [
    "AFTER_GREETING_LINE",
    "DEFAULT_INBOUND_LINE",
    "DEFAULT_OUTBOUND_LINE",
    "Direction",
    "after_greeting_text",
    "disclosure_text",
]


def _default_text(default: str, name: str) -> str:
    return default.replace("{org}", name) if name else _UNNAMED_DEFAULT_LINE


def disclosure_text(
    direction: Direction, org_name: str | None, template: str | None = None
) -> str:
    """The exact line to speak. A blank template means "use the default", and
    so does one whose spoken text would open with tool-call syntax (``[``,
    ``{`` or a backtick): the voicebot would drop it, and the call would
    open with no AI line at all."""
    default = DEFAULT_INBOUND_LINE if direction == "inbound" else DEFAULT_OUTBOUND_LINE
    name = (org_name or "").strip()
    line = (template or "").strip()
    if not line:
        return _default_text(default, name)
    spoken = line.replace("{org}", name or _UNNAMED[direction])
    if spoken.lstrip().startswith(_UNSPEAKABLE_OPENERS):
        return _default_text(default, name)
    return spoken


def after_greeting_text(org_name: str | None) -> str:
    """The short AI line spoken inside an inbound agent's own greeting."""
    name = (org_name or "").strip()
    if not name:
        return _UNNAMED_AFTER_GREETING_LINE
    return AFTER_GREETING_LINE.replace("{org}", name)
