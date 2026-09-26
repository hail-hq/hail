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
    p = tmp_path / "telephony.json"
    p.write_text(json.dumps(data))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(p))
    telephony_catalog._load.cache_clear()  # reset the lru_cache between tests
    return telephony_catalog


def test_capabilities(catalog):
    assert catalog.capabilities("SE", "mobile") == {
        "voice": False,
        "sms": True,
        "mms": False,
    }


def test_missing_file_raises_not_silently_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(tmp_path / "nope.json"))
    telephony_catalog._load.cache_clear()
    with pytest.raises(FileNotFoundError):
        telephony_catalog.capabilities("US", "local")
