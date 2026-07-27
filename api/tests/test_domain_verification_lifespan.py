from __future__ import annotations

from hailhq.core.config import settings


def test_domain_verify_poll_setting_defaults_to_120() -> None:
    assert settings.hail_domain_verify_poll_seconds == 120


def test_main_lifespan_references_domain_verification_worker() -> None:
    import inspect

    from hailhq.api import main

    src = inspect.getsource(main.lifespan)
    assert "DomainVerificationWorker" in src
    assert "hail_domain_verify_poll_seconds" in src
