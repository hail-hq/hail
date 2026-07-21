"""Twilio message-status-callback ``MessageStatus`` → Hail ``SmsStatus``.

Only statuses that represent a persistable transition map to a value; Twilio's
intermediate lifecycle (queued/sending/accepted/scheduled) returns None so the
callback handler skips them without writing or fanning out.
"""

from __future__ import annotations

_MAP: dict[str, str] = {
    "delivered": "delivered",
    "undelivered": "undelivered",
    "failed": "failed",
    "sent": "sent",
}


def map_twilio_message_status(raw: str) -> str | None:
    return _MAP.get(raw.strip().lower())
