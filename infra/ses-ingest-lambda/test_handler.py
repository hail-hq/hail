import hashlib
import hmac
import json
from unittest.mock import patch

import handler

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


def test_handler_posts_signed_payload(monkeypatch):
    monkeypatch.setenv("HAIL_API_URL", "https://api.example.com")
    monkeypatch.setenv("HAIL_INBOUND_BUCKET", "hail-inbound")
    monkeypatch.setenv("HAIL_INBOUND_HMAC_SECRET", "s3cret")

    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["body"] = req.data
        captured["headers"] = dict(req.headers)
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        handler.handler(SES_EVENT, None)

    assert captured["url"] == "https://api.example.com/internal/ses-events"
    payload = json.loads(captured["body"])
    assert payload["message_id"] == "abc123"
    assert payload["envelope_from"] == "alice@example.com"
    assert payload["recipients"] == ["bob+acme@mail.hail.so"]
    assert payload["verdicts"]["spam"] == "PASS"
    assert payload["s3_bucket"] == "hail-inbound"
    assert payload["s3_key"] == "raw/abc123"

    expected = hmac.new(b"s3cret", captured["body"], hashlib.sha256).hexdigest()
    # urllib lower-cases header keys when storing.
    assert captured["headers"].get("X-hail-signature") == f"sha256={expected}"


def test_handler_handles_missing_verdicts(monkeypatch):
    """If SES omits a verdict block (unusual but possible), we send None."""
    monkeypatch.setenv("HAIL_API_URL", "https://api.example.com")
    monkeypatch.setenv("HAIL_INBOUND_BUCKET", "hail-inbound")
    monkeypatch.setenv("HAIL_INBOUND_HMAC_SECRET", "s3cret")

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
    captured = {}

    class FakeResp:
        def __enter__(self):
            return self

        def __exit__(self, *_a):
            pass

        def read(self):
            return b""

    def fake_urlopen(req, timeout):
        captured["body"] = req.data
        return FakeResp()

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        handler.handler(event, None)

    payload = json.loads(captured["body"])
    assert payload["verdicts"] == {
        "spam": None,
        "virus": None,
        "spf": None,
        "dkim": None,
        "dmarc": None,
    }
