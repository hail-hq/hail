import json

import pytest
from hailhq.core import telephony_catalog


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    data = {
        "version": 2,
        "license": "CC-BY-4.0",
        "numbers": [
            {
                "country_code": "SE",
                "number_type": "mobile",
                "usd_per_month": "3.00",
                "voice": False,
                "sms": True,
                "mms": False,
            },
            {
                "country_code": "US",
                "number_type": "local",
                "usd_per_month": "1.15",
                "voice": True,
                "sms": True,
                "mms": True,
            },
            {
                "country_code": "PT",
                "number_type": "local",
                "usd_per_month": "1.00",
                "voice": True,
                "sms": False,
                "mms": False,
                "available": False,
            },
        ],
        "a2p_10dlc": [],
    }
    (tmp_path / "twilio.json").write_text(json.dumps(data))
    telnyx_only = {
        "country_code": "AF",
        "number_type": "local",
        "usd_per_month": "5.00",
        "voice": True,
        "sms": False,
        "mms": False,
    }
    (tmp_path / "telnyx.json").write_text(
        json.dumps({**data, "numbers": [telnyx_only]})
    )
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()  # reset the lru_cache between tests
    return telephony_catalog


def test_capabilities(catalog):
    assert catalog.capabilities("SE", "mobile") == {
        "voice": False,
        "sms": True,
        "mms": False,
    }


def test_capabilities_come_from_the_requested_carrier(catalog):
    """A type only one carrier sells: its own catalog answers, another
    carrier's says no, and 'auto' finds it."""
    caps = {"voice": True, "sms": False, "mms": False}
    assert catalog.capabilities("AF", "local", "telnyx") == caps
    assert catalog.capabilities("AF", "local", "twilio") is None
    assert catalog.capabilities("AF", "local") == caps
    assert catalog.capabilities("SE", "mobile", "telnyx") is None


def test_missing_file_raises_not_silently_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path / "nope"))
    telephony_catalog._load.cache_clear()
    with pytest.raises(FileNotFoundError):
        telephony_catalog.capabilities("US", "local")


def test_calls_only_carrier_never_promises_sms(tmp_path, monkeypatch):
    def row(kind, **over):
        return {
            "country_code": "CA",
            "number_type": kind,
            "usd_per_month": "1.20",
            "voice": True,
            "sms": True,
            "mms": True,
            **over,
        }

    empty = {"numbers": [], "a2p_10dlc": []}
    (tmp_path / "twilio.json").write_text(json.dumps(empty))
    (tmp_path / "telnyx.json").write_text(json.dumps(empty))
    (tmp_path / "didww.json").write_text(
        json.dumps({"numbers": [row("local"), row("mobile", voice=False)]})
    )
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()
    try:
        calls_only = {"voice": True, "sms": False, "mms": False}
        assert telephony_catalog.capabilities("CA", "local", "didww") == calls_only
        assert telephony_catalog.capabilities("CA", "local") == calls_only
        # An SMS-only number at a calls-only carrier is not sold at all.
        assert telephony_catalog.capabilities("CA", "mobile", "didww") is None
    finally:
        telephony_catalog._load.cache_clear()


def test_a_number_hail_cannot_sell_is_not_offered(tmp_path, monkeypatch):
    """A receive-only number, or one the carrier picks at order time, is
    never quoted or bought through the API. Another carrier that sells the
    same kind still answers."""

    def row(kind, **over):
        return {
            "country_code": "PT",
            "number_type": kind,
            "usd_per_month": "3.50",
            "voice": True,
            "sms": False,
            "mms": False,
            **over,
        }

    empty = {"numbers": [], "a2p_10dlc": []}
    (tmp_path / "twilio.json").write_text(json.dumps(empty))
    (tmp_path / "telnyx.json").write_text(json.dumps({"numbers": [row("toll_free")]}))
    (tmp_path / "didww.json").write_text(
        json.dumps(
            {
                "numbers": [
                    row("national", by_request=True),
                    row("toll_free", receive_only=True),
                    row("mobile"),
                ]
            }
        )
    )
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()
    try:
        calls_only = {"voice": True, "sms": False, "mms": False}
        assert telephony_catalog.capabilities("PT", "national", "didww") is None
        assert telephony_catalog.capabilities("PT", "national") is None
        assert telephony_catalog.capabilities("PT", "toll_free", "didww") is None
        assert telephony_catalog.capabilities("PT", "toll_free") == calls_only
        assert telephony_catalog.capabilities("PT", "mobile", "didww") == calls_only
    finally:
        telephony_catalog._load.cache_clear()


def test_sells_in_known_country() -> None:
    assert telephony_catalog.sells_in("US") is True
    assert telephony_catalog.sells_in("US", "twilio") is True


def test_sells_in_unknown_country() -> None:
    assert telephony_catalog.sells_in("ZZ") is False
    assert telephony_catalog.sells_in("ZZ", "twilio") is False


def test_sells_in_ignores_receive_only_and_by_request(tmp_path, monkeypatch):
    def row(kind, **over):
        return {
            "country_code": "PT",
            "number_type": kind,
            "usd_per_month": "3.50",
            "voice": True,
            "sms": False,
            "mms": False,
            **over,
        }

    empty = {"numbers": [], "a2p_10dlc": []}
    for name in ("twilio", "telnyx"):
        (tmp_path / f"{name}.json").write_text(json.dumps(empty))
    (tmp_path / "didww.json").write_text(
        json.dumps(
            {
                "numbers": [
                    row("national", by_request=True),
                    row("toll_free", receive_only=True),
                ]
            }
        )
    )
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    telephony_catalog._load.cache_clear()
    try:
        assert telephony_catalog.sells_in("PT") is False
        assert telephony_catalog.sells_in("PT", "didww") is False
    finally:
        telephony_catalog._load.cache_clear()
