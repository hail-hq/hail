"""Sender ID corridor classification and resolution for outbound SMS.

Only the corridors researched for the SMS design spec are classified —
US, Canada, UK, Germany, Australia, India. Any other destination
conservatively requires the org's dedicated number rather than guessing
at unresearched local Sender ID rules. Extend ``_PREFIX_OUTCOMES`` as more
countries get researched.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = ["SenderResolution", "resolve_sender"]

PLATFORM_DEFAULT_SENDER_ID = "HAIL"

CorridorOutcome = Literal[
    "always_number", "custom_ok", "platform_default_only", "excluded"
]

# Keyed by E.164 country-calling-code prefix. ``+1`` (US and Canada) is
# handled separately below since it resolves to ``always_number`` either
# way — the distinction between the two NANP countries doesn't change
# behavior here.
_PREFIX_OUTCOMES: dict[str, CorridorOutcome] = {
    "49": "custom_ok",  # Germany — no pre-registration required
    "44": "custom_ok",  # UK — no pre-registration for non-"protected" names
    "61": "platform_default_only",  # Australia — ACMA register requires pre-registration
    "91": "excluded",  # India — Twilio silently overwrites alphanumeric with a short code
}


@dataclass
class SenderResolution:
    kind: Literal["dedicated_number_required", "alphanumeric"]
    sender_id: str | None = None


def _classify(to_e164: str) -> CorridorOutcome:
    digits = to_e164.lstrip("+")
    if digits.startswith("1"):
        return "always_number"  # US and Canada resolve the same way here
    for prefix, outcome in _PREFIX_OUTCOMES.items():
        if digits.startswith(prefix):
            return outcome
    return "always_number"  # unresearched corridor — conservative fallback


def resolve_sender(to_e164: str, custom_sender_id: str | None) -> SenderResolution:
    outcome = _classify(to_e164)

    if outcome == "always_number" or outcome == "excluded":
        return SenderResolution(kind="dedicated_number_required")

    if outcome == "platform_default_only":
        return SenderResolution(
            kind="alphanumeric", sender_id=PLATFORM_DEFAULT_SENDER_ID
        )

    # custom_ok
    return SenderResolution(
        kind="alphanumeric", sender_id=custom_sender_id or PLATFORM_DEFAULT_SENDER_ID
    )
