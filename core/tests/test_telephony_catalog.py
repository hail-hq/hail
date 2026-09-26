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
