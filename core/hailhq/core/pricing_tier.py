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

# NANP area codes belonging to countries that are neither the US nor Canada.
# The +1 country code spans ~20 sovereign states and non-US territories whose
# wholesale SMS cost is rest-of-world-shaped, not US-shaped, so they must be
# caught before the US fallthrough below.
#
# US territories (340 USVI, 670 CNMI, 671 Guam, 684 American Samoa, 787/939
# Puerto Rico) are deliberately absent: carriers rate them as US domestic.
_ROW_NANP_AREA_CODES = frozenset(
    {
        "242",  # Bahamas
        "246",  # Barbados
        "264",  # Anguilla
        "268",  # Antigua and Barbuda
        "284",  # British Virgin Islands
        "345",  # Cayman Islands
        "441",  # Bermuda
        "473",  # Grenada
        "649",  # Turks and Caicos Islands
        "658",  # Jamaica (overlay on 876)
        "664",  # Montserrat
        "721",  # Sint Maarten
        "758",  # Saint Lucia
        "767",  # Dominica
        "784",  # Saint Vincent and the Grenadines
        "809",  # Dominican Republic
        "829",  # Dominican Republic
        "849",  # Dominican Republic
        "868",  # Trinidad and Tobago
        "869",  # Saint Kitts and Nevis
        "876",  # Jamaica
    }
)

# Canadian NPAs, owned solely by this module. (sender_id.py deliberately has NO
# such list — US and Canada resolve identically there — so there is nothing to
# keep in sync; billing tiers and Sender-ID corridors are separate partitions of
# the world by design.)
#
# An unlisted +1 NPA falls through to "us", which is the safe default only
# because _ROW_NANP_AREA_CODES above already removed the foreign ones: what
# remains of the NANP really is US-or-Canada, and US outnumbers Canada ~9:1.
# A missing entry here therefore costs the ca/us spread (1¢/segment), not the
# row/us spread. Keep this list current with CNA relief plans.
_CANADIAN_AREA_CODES = frozenset(
    {
        "204",
        "226",
        "236",
        "249",
        "250",
        "263",
        "289",
        "306",
        "343",
        "354",
        "365",
        "367",
        "368",
        "382",
        "403",
        "416",
        "418",
        "428",
        "431",
        "437",
        "438",
        "450",
        "468",
        "474",
        "506",
        "514",
        "519",
        "548",
        "579",
        "581",
        "584",
        "587",
        "604",
        "613",
        "639",
        "647",
        "672",
        "683",
        "705",
        "709",
        "742",
        "753",
        "778",
        "780",
        "782",
        "807",
        "819",
        "825",
        "867",
        "873",
        "879",
        "902",
        "905",
        "942",
    }
)


def classify_pricing_tier(to_e164: str) -> PricingTier:
    digits = to_e164.lstrip("+")
    if digits.startswith("1"):
        area_code = digits[1:4]
        if area_code in _ROW_NANP_AREA_CODES:
            return "row"
        return "ca" if area_code in _CANADIAN_AREA_CODES else "us"
    return "row"
