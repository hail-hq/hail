import pytest
from pydantic import ValidationError
from hailhq.core.schemas import WebhookSubscriptionCreate

NEW_EVENTS = [
    "call.answered",
    "call.completed",
    "call.failed",
    "call.busy",
    "call.no_answer",
    "sms.delivered",
    "sms.undelivered",
    "sms.failed",
    "email.send_failed",
]


@pytest.mark.parametrize("event", NEW_EVENTS)
def test_subscription_accepts_new_event(event):
    sub = WebhookSubscriptionCreate(
        target_url="https://x.test/hook", event_types=[event]
    )
    assert sub.event_types == [event]


def test_subscription_rejects_unknown_event():
    with pytest.raises(ValidationError):
        WebhookSubscriptionCreate(
            target_url="https://x.test/hook", event_types=["call.ringing"]
        )
