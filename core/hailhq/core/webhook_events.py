"""Webhook event names and display metadata owned by the API.

Adding a member updates both subscription validation and the event catalog.
"""

from enum import StrEnum
from typing import Self


class WebhookEventType(StrEnum):
    label: str
    channel: str

    def __new__(cls, value: str, label: str, channel: str) -> Self:
        member = str.__new__(cls, value)
        member._value_ = value
        member.label = label
        member.channel = channel
        return member

    EMAIL_RECEIVED = ("email.received", "Email received", "Email")
    EMAIL_DELIVERED = ("email.delivered", "Email delivered", "Email")
    EMAIL_DELIVERY_DELAYED = (
        "email.delivery_delayed",
        "Email delivery delayed",
        "Email",
    )
    EMAIL_BOUNCED = ("email.bounced", "Email bounced", "Email")
    EMAIL_COMPLAINED = ("email.complained", "Spam complaint", "Email")
    EMAIL_OPENED = ("email.opened", "Email opened", "Email")
    EMAIL_CLICKED = ("email.clicked", "Email link clicked", "Email")
    EMAIL_RECEIVED_SUPPRESSED = (
        "email.received.suppressed",
        "Email forwarding suppressed",
        "Email",
    )
    EMAIL_SEND_FAILED = ("email.send_failed", "Email send failed", "Email")
    SMS_RECEIVED = ("sms.received", "SMS received", "SMS")
    SMS_DELIVERED = ("sms.delivered", "SMS delivered", "SMS")
    SMS_UNDELIVERED = ("sms.undelivered", "SMS undelivered", "SMS")
    SMS_FAILED = ("sms.failed", "SMS failed", "SMS")
    CALL_RECEIVED = ("call.received", "Call received", "Calls")
    CALL_ANSWERED = ("call.answered", "Call answered", "Calls")
    CALL_COMPLETED = ("call.completed", "Call completed", "Calls")
    CALL_FAILED = ("call.failed", "Call failed", "Calls")
    CALL_BUSY = ("call.busy", "Busy line", "Calls")
    CALL_NO_ANSWER = ("call.no_answer", "No answer", "Calls")
    CALL_TRANSFERRED = ("call.transferred", "Call transferred", "Calls")
    CALL_ANALYZED = ("call.analyzed", "Call transcript and summary", "Calls")
