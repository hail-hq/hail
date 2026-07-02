"""Parser for SES configuration-set delivery/engagement events.

These arrive via SNS (config set event destination), relayed by the
ses-ingest-lambda inside a ``{"type": "delivery_event", "event": {...}}``
envelope. This module parses the inner raw SES event into a neutral
``DeliveryEvent``; matching to an Email row and side effects live in
``hailhq.core.email_delivery_events``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

__all__ = ["DeliveryEvent", "parse_delivery_event"]

# SES eventType → our email_events.kind. "Send" is deliberately absent:
# the API writes a synthetic ``sent`` row at send time (Task 5).
_KIND_BY_EVENT_TYPE = {
    "Delivery": "delivered",
    "Bounce": "bounced",
    "Complaint": "complained",
    "Reject": "rejected",
    "DeliveryDelay": "delivery_delayed",
    "Open": "opened",
    "Click": "clicked",
}


@dataclass(frozen=True)
class DeliveryEvent:
    kind: str
    provider_message_id: str
    occurred_at: datetime
    detail: dict[str, Any]


def _ts(value: str | None, fallback: str) -> datetime:
    raw = value or fallback
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _recipients(entries: list[dict] | None) -> list[str]:
    return [e["emailAddress"] for e in (entries or []) if e.get("emailAddress")]


def parse_delivery_event(event: dict[str, Any]) -> DeliveryEvent | None:
    kind = _KIND_BY_EVENT_TYPE.get(event.get("eventType", ""))
    if kind is None:
        return None
    mail = event["mail"]
    mail_ts: str = mail["timestamp"]

    detail: dict[str, Any]
    ts: datetime
    if kind == "delivered":
        d = event.get("delivery") or {}
        ts = _ts(d.get("timestamp"), mail_ts)
        detail = {
            "recipients": list(d.get("recipients") or []),
            "smtp_response": d.get("smtpResponse"),
            "processing_time_ms": d.get("processingTimeMillis"),
        }
    elif kind == "bounced":
        b = event.get("bounce") or {}
        ts = _ts(b.get("timestamp"), mail_ts)
        recips = b.get("bouncedRecipients") or []
        detail = {
            # Provider-neutral hard/soft flag: consumers (status transitions,
            # stats) key off this, not SES's literal bounceType vocabulary.
            "hard": b.get("bounceType") == "Permanent",
            "bounce_type": b.get("bounceType"),
            "bounce_sub_type": b.get("bounceSubType"),
            "recipients": _recipients(recips),
            "diagnostic_code": next(
                (r["diagnosticCode"] for r in recips if r.get("diagnosticCode")),
                None,
            ),
        }
    elif kind == "complained":
        c = event.get("complaint") or {}
        ts = _ts(c.get("timestamp"), mail_ts)
        detail = {
            "complaint_feedback_type": c.get("complaintFeedbackType"),
            "recipients": _recipients(c.get("complainedRecipients")),
        }
    elif kind == "rejected":
        detail = {"reason": (event.get("reject") or {}).get("reason")}
        ts = _ts(None, mail_ts)
    elif kind == "delivery_delayed":
        dd = event.get("deliveryDelay") or {}
        ts = _ts(dd.get("timestamp"), mail_ts)
        detail = {
            "delay_type": dd.get("delayType"),
            "expiration_time": dd.get("expirationTime"),
            "recipients": _recipients(dd.get("delayedRecipients")),
        }
    else:  # opened / clicked
        o = event.get("open" if kind == "opened" else "click") or {}
        ts = _ts(o.get("timestamp"), mail_ts)
        detail = {
            "ip_address": o.get("ipAddress"),
            "user_agent": o.get("userAgent"),
        }
        if kind == "clicked":
            detail["link"] = o.get("link")

    return DeliveryEvent(
        kind=kind,
        provider_message_id=mail["messageId"],
        occurred_at=ts,
        detail=detail,
    )
