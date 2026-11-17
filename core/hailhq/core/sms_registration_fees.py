"""Carrier costs for US A2P 10DLC registration, read from
``costs/sms-registration.json``. Hail's prices are not here: Hail Cloud's
website prices and charges each step. The API uses these costs only as the
balance a customer needs before a billed step: the filing, then the
campaign."""

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


def _fees(provider: str, subject_type: str, low_volume: bool) -> dict:
    """The fee row: ``business_low_volume`` for a company that chose low
    volume (no brand vetting, LOW_VOLUME campaign), else the subject's own.
    A person has no low-volume row. Raises KeyError for a carrier with no
    SMS registration."""
    row = _load()[provider]
    if low_volume and subject_type == "business":
        return row["business_low_volume"]
    return row[subject_type]


def filing_fee_cents(
    provider: str,
    subject_type: str,
    brand_kept: bool = False,
    *,
    low_volume: bool = False,
    faster_lane: bool = False,
) -> int:
    """The carrier's one-time cost of a filing: brand plus campaign vetting,
    or vetting alone for a filing with a kept brand. ``faster_lane``: a
    low-volume company that allowed it, so the worst case adds the brand
    vetting (``faster_lane_fee_cents``) where the carrier has one. Raises
    KeyError for a carrier with no SMS registration."""
    row = _fees(provider, subject_type, low_volume)
    vetting = _cents(row["campaign_vetting_usd"])
    total = vetting if brand_kept else _cents(row["brand_usd"]) + vetting
    if faster_lane and low_volume and subject_type == "business":
        total += _cents(row.get("faster_lane_usd", "0"))
    return total


def brand_fee_cents(
    provider: str, subject_type: str, *, low_volume: bool = False
) -> int:
    """The carrier's one-time cost of a new brand (the submission; for a
    vetted company it includes the brand's vetting). Raises KeyError for a
    carrier with no SMS registration."""
    return _cents(_fees(provider, subject_type, low_volume)["brand_usd"])


def campaign_fee_cents(
    provider: str, subject_type: str, *, low_volume: bool = False
) -> int:
    """The carrier's one-time cost of filing the campaign (its vetting), the
    step after the brand. Raises KeyError for a carrier with no SMS
    registration."""
    return _cents(_fees(provider, subject_type, low_volume)["campaign_vetting_usd"])


def faster_lane_fee_cents(provider: str) -> int:
    """The carrier's cost of the faster lane: vetting a low-volume company
    brand it could not verify. Raises KeyError for a carrier with no faster
    lane (no ``faster_lane_usd``) or no SMS registration."""
    return _cents(_load()[provider]["business_low_volume"]["faster_lane_usd"])
