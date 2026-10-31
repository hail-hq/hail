"""The SES client's limits: every SES call shares them."""

from __future__ import annotations

from hailhq.core.config import settings
from hailhq.core.providers.email import ses


def test_the_client_bounds_a_send(monkeypatch) -> None:
    monkeypatch.setattr(settings, "aws_region", "eu-west-1")
    monkeypatch.setattr(settings, "aws_access_key_id", "test")
    monkeypatch.setattr(settings, "aws_secret_access_key", "test")
    config = ses._build_default_client().meta.config
    assert config.connect_timeout == ses.SES_CONNECT_TIMEOUT_SECONDS == 10
    # Large attachment sends need the full read timeout.
    assert config.read_timeout == ses.SES_READ_TIMEOUT_SECONDS == 60
    assert config.retries["total_max_attempts"] == ses.SES_MAX_ATTEMPTS == 2
    assert config.retries["mode"] == "standard"
