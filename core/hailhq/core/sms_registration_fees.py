"""Carrier fees for US A2P 10DLC registration, read from
``costs/sms-registration.json`` and passed through to the customer at cost."""

from __future__ import annotations

import json
from decimal import Decimal
from functools import lru_cache

from hailhq.core import telephony_catalog

_FILE = "sms-registration"


@lru_cache(maxsize=1)
def _load() -> dict[str, dict]:
    data = json.loads(telephony_catalog._path(_FILE).read_text())
    return {row["provider"]: row for row in data["providers"]}


def _cents(value: str) -> int:
    return int((Decimal(value) * 100).to_integral_value())


def providers() -> list[str]:
    """Carriers that have an SMS registration fee."""
    return list(_load())


def filing_fee_cents(provider: str, subject_type: str) -> int:
    """One-time fee charged when the registration is filed: brand plus
    campaign vetting. Raises KeyError for a carrier with no SMS registration."""
    row = _load()[provider][subject_type]
    return _cents(row["brand_usd"]) + _cents(row["campaign_vetting_usd"])


def refused_brand_fee_cents(provider: str, subject_type: str) -> int:
    """Charged when the carrier registers the brand and then refuses it at
    filing: the carrier bills the brand anyway. 0 when the carrier lists none."""
    return _cents(_load()[provider][subject_type].get("refused_brand_usd", "0"))


def monthly_fee_cents(provider: str, subject_type: str) -> int:
    row = _load()[provider][subject_type]
    return _cents(row["campaign_monthly_usd"])


def minimum_months(provider: str, subject_type: str) -> int:
    """Monthly fees charged at least, from approval, even when the
    registration is cancelled sooner. A row without the field means 1."""
    return int(_load()[provider][subject_type].get("minimum_months", 1))
