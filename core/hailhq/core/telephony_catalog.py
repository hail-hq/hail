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

__all__ = ["capabilities"]

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


def capabilities(
    country_code: str, number_type: str, provider: str = "auto"
) -> dict | None:
    """What a number of this kind can do at ``provider``, or None when that
    carrier does not sell it. 'auto' answers from the first carrier the API
    can buy from that lists it."""
    # Imported here: carrier_routing imports the carrier adapters, and
    # number_offers imports carrier_routing.
    from hailhq.core.carrier_routing import CARRIERS
    from hailhq.core.number_offers import PROVIDERS

    # Only carriers the API can buy from.
    providers = PROVIDERS if provider == "auto" else (provider,)
    for name in providers:
        row = _load(name).get((country_code, number_type))
        if not row:
            continue
        # Hail routes no SMS through this carrier. Its catalog row may list
        # SMS, because the carrier sells it; Hail cannot deliver it, so it
        # is never promised.
        if name in CARRIERS and CARRIERS[name].sms_route is None:
            if not row["voice"]:
                continue  # an SMS-only number Hail cannot use at this carrier
            return {"voice": True, "sms": False, "mms": False}
        return {"voice": row["voice"], "sms": row["sms"], "mms": row["mms"]}
    return None
