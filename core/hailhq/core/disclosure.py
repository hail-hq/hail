"""The spoken AI line.

Spoken by the voicebot as the first thing on every call unless the agent or
the request opted out. The text is a template with one placeholder,
``{org}``, the organization's display name. Precedence, resolved by the
caller: the agent's line, else the workspace line
(``organization_call_settings.ai_disclosure_line``), else the built-in line
for the call's direction. Only admins reach the templates (agents and call
settings), never the public ``POST /calls`` body.
"""

from __future__ import annotations

from typing import Literal

Direction = Literal["outbound", "inbound"]

DEFAULT_OUTBOUND_LINE = "Hi, this is an AI assistant calling on behalf of {org}."
DEFAULT_INBOUND_LINE = "Hi, this is an AI assistant answering on behalf of {org}."

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
    """The exact line to speak. A blank template means "use the default"."""
    text = (template or "").strip() or (
        DEFAULT_INBOUND_LINE if direction == "inbound" else DEFAULT_OUTBOUND_LINE
    )
    name = (org_name or "").strip() or _UNNAMED[direction]
    return text.replace("{org}", name)
