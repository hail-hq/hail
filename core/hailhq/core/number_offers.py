"""Live carrier inventory, prices and regulatory readiness. No country winners table."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from hailhq.core.carrier_offer import CarrierOffer
from hailhq.core.providers.telnyx import get_http_client
from hailhq.core.providers.voice.telnyx import telnyx_offers
from hailhq.core.providers.voice.twilio import twilio_offers
from hailhq.core.schemas import NumberType

logger = logging.getLogger(__name__)

__all__ = [
    "CarrierOffer",
    "discover_offers",
    "rank_offers",
    "telnyx_offers",
    "twilio_offers",
]


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
            o.provider != "twilio",
            o.e164,
        ),
    )


PROVIDERS = ("twilio", "telnyx")


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
    searches = {
        "twilio": lambda: twilio_offers(org, country, kind, capabilities, e164=e164),
        "telnyx": lambda: telnyx_offers(
            org, country, kind, capabilities, get_http_client(), e164=e164
        ),
    }
    asked = [p for p in PROVIDERS if p in providers]
    results = await asyncio.gather(
        *(searches[p]() for p in asked), return_exceptions=True
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
