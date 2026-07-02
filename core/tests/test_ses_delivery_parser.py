"""Parser tests for SES configuration-set delivery events."""

from datetime import datetime, timezone

from hailhq.core.providers.email.inbound.ses_delivery import parse_delivery_event

MAIL = {
    "messageId": "0107019-real-ses-message-id-000",
    "timestamp": "2026-07-01T12:00:00.000Z",
    "source": "noreply@acme.com",
    "destination": ["bob@example.com"],
}


def test_parse_delivery():
    ev = parse_delivery_event(
        {
            "eventType": "Delivery",
            "mail": MAIL,
            "delivery": {
                "timestamp": "2026-07-01T12:00:03.000Z",
                "recipients": ["bob@example.com"],
                "smtpResponse": "250 2.6.0 OK",
                "processingTimeMillis": 3000,
            },
        }
    )
    assert ev is not None
    assert ev.kind == "delivered"
    assert ev.provider_message_id == MAIL["messageId"]
    assert ev.occurred_at == datetime(2026, 7, 1, 12, 0, 3, tzinfo=timezone.utc)
    assert ev.detail["smtp_response"] == "250 2.6.0 OK"
    assert ev.detail["recipients"] == ["bob@example.com"]


def test_parse_permanent_bounce():
    ev = parse_delivery_event(
        {
            "eventType": "Bounce",
            "mail": MAIL,
            "bounce": {
                "bounceType": "Permanent",
                "bounceSubType": "General",
                "timestamp": "2026-07-01T12:00:05.000Z",
                "bouncedRecipients": [
                    {
                        "emailAddress": "bob@example.com",
                        "diagnosticCode": "smtp; 550 5.1.1 user unknown",
                    }
                ],
            },
        }
    )
    assert ev.kind == "bounced"
    assert ev.detail["hard"] is True
    assert ev.detail["bounce_type"] == "Permanent"
    assert ev.detail["bounce_sub_type"] == "General"
    assert ev.detail["recipients"] == ["bob@example.com"]
    assert ev.detail["diagnostic_code"] == "smtp; 550 5.1.1 user unknown"


def test_parse_transient_bounce_is_soft():
    ev = parse_delivery_event(
        {
            "eventType": "Bounce",
            "mail": MAIL,
            "bounce": {
                "bounceType": "Transient",
                "bounceSubType": "MailboxFull",
                "timestamp": "2026-07-01T12:00:05.000Z",
                "bouncedRecipients": [{"emailAddress": "bob@example.com"}],
            },
        }
    )
    assert ev.detail["hard"] is False
    assert ev.detail["bounce_type"] == "Transient"


def test_parse_complaint():
    ev = parse_delivery_event(
        {
            "eventType": "Complaint",
            "mail": MAIL,
            "complaint": {
                "timestamp": "2026-07-01T12:01:00.000Z",
                "complaintFeedbackType": "abuse",
                "complainedRecipients": [{"emailAddress": "bob@example.com"}],
            },
        }
    )
    assert ev.kind == "complained"
    assert ev.detail["complaint_feedback_type"] == "abuse"


def test_parse_reject():
    ev = parse_delivery_event(
        {
            "eventType": "Reject",
            "mail": MAIL,
            "reject": {"reason": "Bad content"},
        }
    )
    assert ev.kind == "rejected"
    assert ev.detail["reason"] == "Bad content"
    # Reject carries no event timestamp — falls back to mail.timestamp.
    assert ev.occurred_at == datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def test_parse_delivery_delay():
    ev = parse_delivery_event(
        {
            "eventType": "DeliveryDelay",
            "mail": MAIL,
            "deliveryDelay": {
                "timestamp": "2026-07-01T12:02:00.000Z",
                "delayType": "MailboxFull",
                "expirationTime": "2026-07-02T12:00:00.000Z",
                "delayedRecipients": [{"emailAddress": "bob@example.com"}],
            },
        }
    )
    assert ev.kind == "delivery_delayed"
    assert ev.detail["delay_type"] == "MailboxFull"


def test_parse_open_and_click():
    op = parse_delivery_event(
        {
            "eventType": "Open",
            "mail": MAIL,
            "open": {
                "timestamp": "2026-07-01T12:05:00.000Z",
                "ipAddress": "203.0.113.9",
                "userAgent": "Mozilla/5.0",
            },
        }
    )
    assert op.kind == "opened"
    assert op.detail["user_agent"] == "Mozilla/5.0"

    cl = parse_delivery_event(
        {
            "eventType": "Click",
            "mail": MAIL,
            "click": {
                "timestamp": "2026-07-01T12:06:00.000Z",
                "ipAddress": "203.0.113.9",
                "userAgent": "Mozilla/5.0",
                "link": "https://acme.com/offer",
            },
        }
    )
    assert cl.kind == "clicked"
    assert cl.detail["link"] == "https://acme.com/offer"


def test_untracked_event_type_returns_none():
    assert parse_delivery_event({"eventType": "Send", "mail": MAIL}) is None
    assert (
        parse_delivery_event({"eventType": "Rendering Failure", "mail": MAIL}) is None
    )
