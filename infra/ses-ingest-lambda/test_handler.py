import hashlib
import hmac
import json

import handler
import pytest

SES_EVENT = {
    "Records": [
        {
            "ses": {
                "mail": {
                    "messageId": "abc123",
                    "source": "alice@example.com",
                    "timestamp": "2026-06-06T10:11:12Z",
                },
                "receipt": {
                    "recipients": ["bob+acme@mail.hail.so"],
                    "spamVerdict": {"status": "PASS"},
                    "virusVerdict": {"status": "PASS"},
                    "spfVerdict": {"status": "PASS"},
                    "dkimVerdict": {"status": "PASS"},
                    "dmarcVerdict": {"status": "PASS"},
                },
            }
        }
    ]
}


class CapturedRequest:
    """Holder for captured request data and headers."""

    def __init__(self):
        self.url = None
        self.data = None
        self.headers = {}


@pytest.fixture
def captured_request(monkeypatch):
    """Fixture that captures urlopen calls into a CapturedRequest object."""
    req_obj = CapturedRequest()

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        req_obj.url = req.full_url
        req_obj.data = req.data
        req_obj.headers = dict(req.headers)
        return FakeResp()

    monkeypatch.setenv("HAIL_API_URL", "https://api.example.com")
    monkeypatch.setenv("HAIL_INBOUND_BUCKET", "hail-inbound")
    monkeypatch.setenv("HAIL_INBOUND_HMAC_SECRET", "s3cret")
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    return req_obj


def test_handler_posts_signed_payload(captured_request):
    handler.handler(SES_EVENT, None)

    assert captured_request.url == "https://api.example.com/internal/ses-events"
    payload = json.loads(captured_request.data)
    assert payload["message_id"] == "abc123"
    assert payload["envelope_from"] == "alice@example.com"
    assert payload["recipients"] == ["bob+acme@mail.hail.so"]
    assert payload["verdicts"]["spam"] == "PASS"
    assert payload["s3_bucket"] == "hail-inbound"
    assert payload["s3_key"] == "raw/abc123"

    expected = hmac.new(b"s3cret", captured_request.data, hashlib.sha256).hexdigest()
    # urllib lower-cases header keys when storing.
    assert captured_request.headers.get("X-hail-signature") == f"sha256={expected}"


def test_handler_handles_missing_verdicts(captured_request):
    """If SES omits a verdict block (unusual but possible), we send None."""
    event = {
        "Records": [
            {
                "ses": {
                    "mail": {
                        "messageId": "no-verdicts",
                        "source": "alice@example.com",
                        "timestamp": "2026-06-06T10:11:12Z",
                    },
                    "receipt": {
                        "recipients": ["bob+acme@mail.hail.so"],
                        # All verdict fields missing.
                    },
                }
            }
        ]
    }
    handler.handler(event, None)

    payload = json.loads(captured_request.data)
    assert payload["verdicts"] == {
        "spam": None,
        "virus": None,
        "spf": None,
        "dkim": None,
        "dmarc": None,
    }


def test_sns_delivery_event_wrapped_and_signed(captured_request):
    ses_event = {
        "eventType": "Delivery",
        "mail": {"messageId": "mid-1", "timestamp": "2026-07-01T12:00:00.000Z"},
        "delivery": {
            "timestamp": "2026-07-01T12:00:03.000Z",
            "recipients": ["b@example.com"],
        },
    }
    sns_record = {"Sns": {"Message": json.dumps(ses_event)}}
    handler.handler({"Records": [sns_record]}, None)

    body = json.loads(captured_request.data)
    assert body["type"] == "delivery_event"
    assert body["event"]["eventType"] == "Delivery"
    # Exact HMAC over the wire bytes — a re-serialization before signing
    # would produce a different digest and fail here.
    expected = hmac.new(b"s3cret", captured_request.data, hashlib.sha256).hexdigest()
    assert captured_request.headers["X-hail-signature"] == f"sha256={expected}"
