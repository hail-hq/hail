from decimal import Decimal
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
        ],
        "a2p_10dlc": [],
    }
    p = tmp_path / "telephony.json"
    p.write_text(json.dumps(data))
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(p))
    telephony_catalog._load.cache_clear()  # reset the lru_cache between tests
    return telephony_catalog


def test_is_acquirable(catalog):
    assert catalog.is_acquirable("SE", "mobile") is True
    assert catalog.is_acquirable("SE", "local") is False  # not listed
    assert catalog.is_acquirable("ZZ", "local") is False


def test_price_and_capabilities(catalog):
    assert catalog.price_usd_per_month("US", "local") == Decimal("1.15")
    assert catalog.price_usd_per_month("SE", "local") is None
    assert catalog.capabilities("SE", "mobile") == {
        "voice": False,
        "sms": True,
        "mms": False,
    }


def test_missing_file_raises_not_silently_allows(tmp_path, monkeypatch):
    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_PATH", str(tmp_path / "nope.json"))
    telephony_catalog._load.cache_clear()
    with pytest.raises(FileNotFoundError):
        telephony_catalog.is_acquirable("US", "local")
