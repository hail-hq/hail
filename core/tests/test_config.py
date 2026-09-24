import pytest
from pydantic import ValidationError


def test_compliance_reply_defaults() -> None:
    from hailhq.core.config import settings

    assert settings.hail_sms_compliance_replies_enabled is False
    assert "STOP" in settings.hail_sms_stop_reply
    assert "hi@hail.so" in settings.hail_sms_help_reply
    assert settings.hail_sms_start_reply


@pytest.mark.parametrize("value", ["0", "-1"])
def test_api_rate_limit_per_minute_rejects_non_positive(
    monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    # 0 would 429 every request; a negative value makes limits.parse raise
    # on every request. Both must fail at startup, not at the first request.
    from hailhq.core.config import Settings

    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_api_rate_limit_per_minute_accepts_positive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from hailhq.core.config import Settings

    monkeypatch.setenv("API_RATE_LIMIT_PER_MINUTE", "5")
    assert Settings(_env_file=None).api_rate_limit_per_minute == 5
