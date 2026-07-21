import pytest
from hailhq.core.providers.sms.status_map import map_twilio_message_status


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("delivered", "delivered"),
        ("undelivered", "undelivered"),
        ("failed", "failed"),
        ("sent", "sent"),
        ("Delivered", "delivered"),  # case-insensitive
    ],
)
def test_maps_terminal_statuses(raw, expected):
    assert map_twilio_message_status(raw) == expected


@pytest.mark.parametrize("raw", ["queued", "sending", "accepted", "scheduled", "weird"])
def test_ignores_intermediate_and_unknown(raw):
    assert map_twilio_message_status(raw) is None
