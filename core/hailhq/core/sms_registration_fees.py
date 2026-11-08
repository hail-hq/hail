"""Carrier costs for US A2P 10DLC registration, read from
``costs/sms-registration.json``. Hail's prices are not here: Hail Cloud's
website prices and charges each step. The API uses the filing cost only as
the balance a customer needs before filing."""

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


def filing_fee_cents(provider: str, subject_type: str, brand_kept: bool = False) -> int:
    """The carrier's one-time cost of a filing: brand plus campaign vetting,
    or vetting alone for a filing with a kept brand. Raises KeyError for a
    carrier with no SMS registration."""
    row = _load()[provider][subject_type]
    vetting = _cents(row["campaign_vetting_usd"])
    return vetting if brand_kept else _cents(row["brand_usd"]) + vetting
