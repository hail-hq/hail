import hashlib
import hmac
import json

from hailhq.core.webhooks import (
    API_VERSION,
    RETRY_SCHEDULE_SECONDS,
    build_event_payload,
    next_attempt_delay,
    sign_payload,
)


def test_sign_payload_uses_dotted_message():
    body = b'{"a":1}'
    secret = "topsecret"
    header = sign_payload(body, secret, timestamp=1717_000_000)
    assert header.startswith("t=1717000000,v1=")
    sig_hex = header.split("v1=")[1]
    expected = hmac.new(
        secret.encode(), b"1717000000." + body, hashlib.sha256
    ).hexdigest()
    assert sig_hex == expected


def test_sign_payload_auto_timestamp_recent():
    import time

    before = int(time.time())
    header = sign_payload(b"x", "s")
    ts = int(header.split("t=")[1].split(",")[0])
    after = int(time.time())
    assert before <= ts <= after


def test_build_event_payload_minimal():
    payload = build_event_payload(
        delivery_id="00000000-0000-0000-0000-000000000001",
        event_type="email.received",
        organization_id="00000000-0000-0000-0000-000000000002",
        data={"id": "x"},
    )
    parsed = json.loads(payload)
    assert parsed["type"] == "email.received"
    assert parsed["api_version"] == API_VERSION
    assert parsed["data"] == {"id": "x"}
    assert parsed["organization_id"] == "00000000-0000-0000-0000-000000000002"


def test_retry_schedule_matches_spec():
    # 0s, 30s, 2m, 10m, 1h, 6h, 24h
    assert RETRY_SCHEDULE_SECONDS == [0, 30, 120, 600, 3600, 21600, 86400]


def test_next_attempt_delay_returns_each_slot():
    for i, expected in enumerate(RETRY_SCHEDULE_SECONDS):
        assert next_attempt_delay(i) == expected
    # Past the end → None (dead).
    assert next_attempt_delay(len(RETRY_SCHEDULE_SECONDS)) is None
