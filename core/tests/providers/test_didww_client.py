import pytest
from didww.configuration import Environment
from didww.exceptions import DidwwApiError
from hailhq.core.config import settings
from hailhq.core.providers.voice import CarrierNotConfigured
from hailhq.core.providers.voice.didww import carrier_status, didww_client


def test_client_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "")
    with pytest.raises(CarrierNotConfigured):
        didww_client()


@pytest.mark.parametrize(
    ("env", "base"),
    [
        ("production", Environment.PRODUCTION.value),
        ("sandbox", Environment.SANDBOX.value),
    ],
)
def test_client_picks_environment(monkeypatch: pytest.MonkeyPatch, env, base) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "k")
    monkeypatch.setattr(settings, "didww_environment", env)
    assert didww_client().base_url == base


def test_client_rejects_unknown_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "didww_api_key", "k")
    monkeypatch.setattr(settings, "didww_environment", "staging")
    with pytest.raises(CarrierNotConfigured):
        didww_client()


def test_carrier_status_defaults_to_502() -> None:
    assert carrier_status(DidwwApiError([{"title": "x"}], status_code=422)) == 422
    assert carrier_status(DidwwApiError([{"title": "x"}])) == 502
