"""Read-only view of the carrier catalogs in costs/ (twilio.json, telnyx.json,
...): number price + capability per (country, number type) for one carrier.
The same files the rater (hail-website) and the /costs page read, so the
three can never disagree about a number's capabilities.

A missing file raises rather than silently returning nothing: the error forces
the deploy to be fixed (the files must be bundled — see api/Dockerfile).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path

__all__ = [
    "by_request_row",
    "capabilities",
    "effective_capabilities",
    "monthly_price",
    "requirements",
    "rows",
    "sells_in",
    "tollfree_sms_unverifiable",
]

# In the API image costs/ is copied to /app/costs (see api/Dockerfile); in dev
# the module sits at core/hailhq/core/ so parents[3] is the repo root. An env
# var overrides both (tests, alternate layouts).
_DEFAULT_DIR = Path(__file__).resolve().parents[3] / "costs"


def _path(provider: str) -> Path:
    return Path(os.environ.get("HAIL_TELEPHONY_CATALOG_DIR", str(_DEFAULT_DIR))) / (
        f"{provider}.json"
    )


@lru_cache(maxsize=8)
def _load(provider: str) -> dict[tuple[str, str], dict]:
    raw = json.loads(_path(provider).read_text())
    return {(n["country_code"], n["number_type"]): n for n in raw["numbers"]}


@lru_cache(maxsize=8)
def _capability_fees(path: Path) -> tuple[dict, ...]:
    return tuple(json.loads(path.read_text()).get("capability_monthly_fees", []))


def _sms_route_exists(provider: str) -> bool:
    from hailhq.core.carrier_routing import CARRIERS

    return provider not in CARRIERS or CARRIERS[provider].sms_route is not None


def tollfree_sms_unverifiable(
    provider: str, country_code: str | None, number_type: str | None
) -> bool:
    """True for a US/CA toll-free number at a carrier Hail cannot verify
    toll-free texting with: it can never text the US or Canada."""
    if country_code not in {"US", "CA"} or number_type != "toll_free":
        return False
    # Imported here: carrier_routing imports the carrier adapters.
    from hailhq.core.carrier_routing import CARRIERS

    entry = CARRIERS.get(provider)
    return not (entry and entry.toll_free_sms_verification)


def effective_capabilities(
    row: dict,
    provider: str,
    country_code: str | None = None,
    number_type: str | None = None,
) -> list[str]:
    """What Hail can deliver on this catalog row: ``voice`` and/or ``sms``.
    Hail routes no SMS through a carrier whose ``sms_route`` is None. Its
    catalog row may list SMS, because the carrier sells it; Hail cannot
    deliver it, so it is never promised. An SMS-only row at such a carrier
    gives an empty list. A US/CA toll-free row at a carrier Hail cannot verify
    for toll-free texting never promises SMS either."""
    caps = []
    if row.get("voice"):
        caps.append("voice")
    if (
        row.get("sms")
        and _sms_route_exists(provider)
        and not tollfree_sms_unverifiable(provider, country_code, number_type)
    ):
        caps.append("sms")
    return caps


def capabilities(
    country_code: str, number_type: str, provider: str = "auto"
) -> dict | None:
    """What a number of this kind can do at ``provider``, or None when that
    carrier does not sell it. 'auto' answers from the first carrier the API
    can buy from that lists it."""
    # Imported here: number_offers imports carrier_routing, which imports
    # the carrier adapters.
    from hailhq.core.number_offers import PROVIDERS

    # Only carriers the API can buy from.
    providers = PROVIDERS if provider == "auto" else (provider,)
    for name in providers:
        row = _load(name).get((country_code, number_type))
        if not row:
            continue
        # Takes calls but cannot place them, or the carrier picks the number
        # at order time: neither can be quoted and bought here. Support
        # orders a by_request number by hand.
        if row.get("receive_only") or row.get("by_request"):
            continue
        caps = effective_capabilities(row, name, country_code, number_type)
        if not caps and not _sms_route_exists(name):
            continue  # an SMS-only number Hail cannot use at this carrier
        # No SMS Hail can deliver (no SMS route, or toll-free SMS Hail cannot
        # verify) means no MMS.
        mms = bool(row.get("mms")) and "sms" in caps
        return {"voice": "voice" in caps, "sms": "sms" in caps, "mms": mms}
    return None


def sells_in(country_code: str, provider: str = "auto") -> bool:
    """True when ``provider`` (or any carrier the API buys from, for 'auto')
    sells a number in ``country_code`` that can place calls: the same test
    as :func:`capabilities`, so receive-only and by-request rows do not
    count. The handover destination guard: a flat per-minute rate only
    covers countries Hail already sells in."""
    from hailhq.core.number_offers import PROVIDERS

    providers = PROVIDERS if provider == "auto" else (provider,)
    number_types = {
        nt for name in providers for cc, nt in _load(name) if cc == country_code
    }
    for number_type in number_types:
        caps = capabilities(country_code, number_type, provider)
        if caps is not None and caps["voice"]:
            return True
    return False


def by_request_row(country_code: str, number_type: str, provider: str) -> dict | None:
    """``provider``'s catalog row for this kind of number when the carrier
    sells it by request only (support orders it by hand), else None."""
    row = _load(provider).get((country_code, number_type))
    return row if row and row.get("by_request") else None


def requirements(country_code: str, number_type: str, provider: str) -> dict | None:
    """The stored requirements block of ``provider``'s catalog row (who may
    buy and which paper categories each needs), or None when the row has no
    block. Read from the catalog only; no carrier call."""
    row = _load(provider).get((country_code, number_type))
    return row.get("requirements") if row else None


def rows(country_code: str, provider: str) -> list[dict]:
    """Every catalog row of ``provider`` in this country, by-request rows
    included, receive-only rows left out."""
    return [
        row
        for (cc, _), row in _load(provider).items()
        if cc == country_code and not row.get("receive_only")
    ]


def monthly_price(provider: str, base_cents: int, selected: list[str]) -> dict:
    """Complete purchase rental, keeping base and selected capability fees separate.

    Inventory prices exclude these provider-published capability fees. Each
    fee is charged once even when several of its capabilities are selected.
    """
    from hailhq.core.carrier_offer import cents

    fees = sum(
        cents(fee["usd_per_month"])
        for fee in _capability_fees(_path(provider))
        if set(selected).intersection(fee["capabilities"])
    )
    return {
        "monthly_price_version": 2,
        "base_monthly_cents": base_cents,
        "capability_monthly_cents": fees,
        "monthly_cents": base_cents + fees,
    }
