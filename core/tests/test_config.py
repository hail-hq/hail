def test_compliance_reply_defaults() -> None:
    from hailhq.core.config import settings

    assert settings.hail_sms_compliance_replies_enabled is False
    assert "STOP" in settings.hail_sms_stop_reply
    assert "hi@hail.so" in settings.hail_sms_help_reply
    assert settings.hail_sms_start_reply
