"""SMS billing-tier classification: US / Canada / rest-of-world.

Distinct from sender_id.py's corridor classification (which answers a
different question — Sender ID eligibility, not billing rate) even
though both parse an E.164 prefix. Keep these separate; do not merge
them just because the input type overlaps.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["classify_pricing_tier"]

PricingTier = Literal["us", "ca", "row"]

# Canadian NPA list, owned solely by this module. (sender_id.py deliberately
# has NO such list — US and Canada resolve identically there — so there is
# nothing to keep in sync; billing tiers and Sender-ID corridors are separate
# partitions of the world by design.)
_CANADIAN_AREA_CODES = frozenset(
    {"204", "226", "236", "249", "250", "289", "306", "343", "365", "387", "403", "416",
     "418", "431", "437", "438", "450", "506", "514", "519", "548", "579", "581", "587",
     "604", "613", "639", "647", "672", "705", "709", "778", "780", "782", "807", "819",
     "825", "867", "873", "902", "905"}
)


def classify_pricing_tier(to_e164: str) -> PricingTier:
    digits = to_e164.lstrip("+")
    if digits.startswith("1"):
        area_code = digits[1:4]
        return "ca" if area_code in _CANADIAN_AREA_CODES else "us"
    return "row"
