"""Read-only view of costs/telephony.json — the number price + capability
catalog and the acquire allow-list. The same file the rater (hail-website) and
the /costs page read, so the three can never disagree about what's acquirable.

A missing file raises rather than silently allowing/denying: an acquire guard
that fails open would let unpriced numbers through and break "price every
number"; one that fails closed would block all acquisition. Surfacing the error
forces the deploy to be fixed (the file must be bundled — see api/Dockerfile).
"""

from __future__ import annotations

import json
import os
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

__all__ = ["capabilities", "is_acquirable", "price_usd_per_month"]

# In the API image costs/ is copied to /app/costs (see api/Dockerfile); in dev
# the module sits at core/hailhq/core/ so parents[3] is the repo root. An env
# var overrides both (tests, alternate layouts).
_DEFAULT_PATH = Path(__file__).resolve().parents[3] / "costs" / "telephony.json"


def _path() -> Path:
    return Path(os.environ.get("HAIL_TELEPHONY_CATALOG_PATH", str(_DEFAULT_PATH)))


@lru_cache(maxsize=1)
def _load() -> dict[tuple[str, str], dict]:
    raw = json.loads(_path().read_text())
    return {(n["country_code"], n["number_type"]): n for n in raw["numbers"]}


def is_acquirable(country_code: str, number_type: str) -> bool:
    return (country_code, number_type) in _load()


def price_usd_per_month(country_code: str, number_type: str) -> Decimal | None:
    row = _load().get((country_code, number_type))
    return Decimal(row["usd_per_month"]) if row else None


def capabilities(country_code: str, number_type: str) -> dict | None:
    row = _load().get((country_code, number_type))
    if not row:
        return None
    return {"voice": row["voice"], "sms": row["sms"], "mms": row["mms"]}
