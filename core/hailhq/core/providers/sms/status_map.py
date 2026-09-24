"""Carrier message-status callbacks → Hail ``SmsStatus``.

Twilio ``MessageStatus``:

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


# Telnyx ``message.finalized`` recipient status. Every value is terminal.
_TELNYX_MAP: dict[str, str] = {
    "delivered": "delivered",
    "delivery_failed": "undelivered",
    "expired": "undelivered",
    "sending_failed": "failed",
}


def map_telnyx_message_status(raw: str | None) -> str | None:
    return _TELNYX_MAP.get(raw) if raw else None
