"""Live carrier inventory, prices and regulatory readiness. No country winners table."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from hailhq.core.carrier_offer import CarrierOffer
from hailhq.core.carrier_routing import CARRIERS, TELNYX, carrier
from hailhq.core.schemas import NumberType

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDERS",
    "CarrierOffer",
    "discover_offers",
    "preferred_provider",
    "rank_offers",
]

# Every carrier, in the order ``CARRIERS`` lists them.
PROVIDERS = tuple(CARRIERS)

# At equal price and readiness, the carrier to buy from per country. US
# numbers go to Telnyx: its US texting registration takes persons and
# companies today. Everywhere else the first carrier in ``CARRIERS`` wins.
_PREFERRED = {"US": TELNYX}


def preferred_provider(country_code: str) -> str:
    return _PREFERRED.get(country_code.upper(), PROVIDERS[0])


def rank_offers(
    offers: list[CarrierOffer], provider: str = "auto"
) -> list[CarrierOffer]:
    """Ready first; lowest remaining verification effort, then rental/setup cost.

    Legally blocked stock is never recommended over activatable stock. Unknown
    prices/readiness are excluded by discovery, never interpreted as free/ready.
    """
    return sorted(
        (o for o in offers if provider == "auto" or o.provider == provider),
        key=lambda o: (
            o.readiness != "ready",
            (
                0
                if o.readiness == "ready"
                else {"none": 0, "information": 1, "documents": 2, "unknown": 3}[
                    o.regulatory_friction
                ]
            ),
            o.monthly_cents,
            o.setup_cents,
            o.provider != preferred_provider(o.country_code),
            o.e164,
        ),
    )


async def discover_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
    providers: list[str] | tuple[str, ...] = PROVIDERS,
) -> tuple[list[CarrierOffer], list[str]]:
    """Live offers from ``providers`` (every carrier by default) and the
    carriers whose lookup failed."""
    asked = [p for p in PROVIDERS if p in providers]
    results = await asyncio.gather(
        *(carrier(p).offers(org, country, kind, capabilities, e164) for p in asked),
        return_exceptions=True,
    )
    offers, unavailable = [], []
    for provider, result in zip(asked, results):
        if isinstance(result, asyncio.CancelledError):
            raise result
        if isinstance(result, BaseException):
            logger.warning(
                "Carrier discovery failed: provider=%s", provider, exc_info=result
            )
            unavailable.append(provider)
        else:
            offers.extend(result)
    return rank_offers(offers), unavailable
