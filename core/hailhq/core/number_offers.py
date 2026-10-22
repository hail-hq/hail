"""Live carrier inventory, prices and regulatory readiness. No country winners table."""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from hailhq.core import telephony_catalog
from hailhq.core.carrier_offer import Approval, CarrierOffer, cents
from hailhq.core.carrier_routing import CARRIERS, carrier
from hailhq.core.schemas import NumberType

logger = logging.getLogger(__name__)

__all__ = [
    "PROVIDERS",
    "CarrierOffer",
    "by_request_offers",
    "discover_offers",
    "rank_offers",
]

# Every carrier, in the order ``CARRIERS`` lists them.
PROVIDERS = tuple(CARRIERS)


def rank_offers(
    offers: list[CarrierOffer], provider: str = "auto"
) -> list[CarrierOffer]:
    """Ready first; lowest remaining verification effort, then rental/setup cost.
    By-request offers (support orders them by hand) come after every offer
    that can be bought.

    Legally blocked stock is never recommended over activatable stock. Unknown
    prices/readiness are excluded by discovery, never interpreted as free/ready.
    """
    return sorted(
        (o for o in offers if provider == "auto" or o.provider == provider),
        key=lambda o: (
            o.by_request,
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
            o.provider != PROVIDERS[0],
            o.e164 or "",
        ),
    )


async def discover_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
    providers: list[str] | tuple[str, ...] = PROVIDERS,
    area_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    approvals: dict[str, dict[str, Approval]] | None = None,
) -> tuple[list[CarrierOffer], list[str]]:
    """Live offers from ``providers`` (every carrier by default) and the
    carriers whose lookup failed. ``approvals`` is the org's approved Hail
    verifications per carrier and subject type (``approved_handles``)."""
    asked = [p for p in PROVIDERS if p in providers]
    approvals = approvals or {}
    results = await asyncio.gather(
        *(
            carrier(p).offers(
                org,
                country,
                kind,
                capabilities,
                e164,
                area_code,
                city,
                region,
                approvals.get(p, {}),
            )
            for p in asked
        ),
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


def by_request_offers(
    country: str,
    kind: NumberType,
    capabilities: list[str],
    configured: list[str] | tuple[str, ...] = PROVIDERS,
) -> list[CarrierOffer]:
    """One offer per carrier in ``configured`` that lists this kind of number
    by request only, from its catalog row. Support orders such a number by
    hand, so there is no number yet (``e164`` is None) and it cannot be
    bought through the API. The offer still gets a quote, so the customer can
    verify for it. Readiness here comes from the catalog alone; the caller
    marks it ready when the org already has an approved verification."""
    out: list[CarrierOffer] = []
    for name in PROVIDERS:
        if name not in configured:
            continue
        row = telephony_catalog.by_request_row(country, kind, name)
        if row is None:
            continue
        sms = bool(row.get("sms")) and CARRIERS[name].sms_route is not None
        has = {"voice": bool(row.get("voice")), "sms": sms}
        if not has["voice"] or not all(has.get(c, False) for c in capabilities):
            continue
        needs = bool(row.get("verification_required"))
        out.append(
            CarrierOffer(
                provider=name,
                e164=None,
                country_code=country,
                number_type=kind,
                capabilities=[c for c in ("voice", "sms") if has[c]],
                monthly_cents=cents(row["usd_per_month"]),
                setup_cents=cents(row.get("setup_usd") or 0),
                readiness="verification_required" if needs else "ready",
                regulatory_friction="unknown" if needs else "none",
                requirements=(
                    ["End-user registration (identity and address)"] if needs else []
                ),
                # The catalog does not say who may verify: either may.
                subject_types=["business", "person"] if needs else [],
                by_request=True,
            )
        )
    return out
