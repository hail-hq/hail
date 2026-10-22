"""Carrier offer model shared by discovery adapters and the purchase flow."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

import phonenumbers
from hailhq.core.schemas import NumberType
from pydantic import BaseModel, Field


class OfferVerification(BaseModel):
    id: UUID = Field(
        description="Hail verification id. Read it with GET /v1/verifications/{verification_id}."
    )
    state: str = Field(
        description="State of that verification, such as draft, submitted or approved."
    )


class QuoteOffer(BaseModel):
    """One number offer as the public API shows it. It never names the
    carrier or carries a carrier-issued id: Hail picks the carrier."""

    e164: str | None = Field(
        description="Available phone number in E.164 format. Null for a by-request offer: support picks the number when it orders it."
    )
    country_code: str = Field(description="ISO alpha-2 country code of the number.")
    number_type: NumberType = Field(
        description="Local, mobile, national, or toll-free number type."
    )
    capabilities: list[str] = Field(
        description="Voice/SMS capabilities reported by live carrier inventory; SMS registration may still be required."
    )
    monthly_cents: int = Field(
        ge=1,
        description="Monthly number rental in USD cents, charged from organization credits.",
    )
    setup_cents: int = Field(
        ge=0,
        description="One-time setup charge in USD cents, payable with the first month.",
    )
    currency: Literal["USD"] = Field(
        default="USD", description="Currency of the quoted rental and setup amounts."
    )
    readiness: Literal["ready", "verification_required"] = Field(
        description="Whether regulatory preflight permits purchase for this organization; rechecked at purchase."
    )
    regulatory_friction: Literal["none", "information", "documents", "unknown"] = Field(
        default="unknown",
        description="Remaining verification effort derived from live requirements: none, information/address entry, document uploads, or unknown. This does not establish legal eligibility.",
    )
    requirements: list[str] = Field(
        default_factory=list,
        description="Carrier regulatory requirement labels associated with this offer.",
    )
    locality: str | None = Field(
        default=None,
        description="City or area the carrier lists for this number, such as Phoenix or London; null when unknown.",
    )
    region: str | None = Field(
        default=None,
        description="State, province or region the carrier lists, such as AZ or England; null when unknown.",
    )
    quote_id: UUID | None = Field(
        default=None,
        description="Organization-bound quote identifier to pass to POST /numbers before expiry.",
    )
    verification: OfferVerification | None = Field(
        default=None,
        description="Your organization's verification for this offer's country and number type that is not cancelled or rejected; null when there is none.",
    )
    by_request: bool = Field(
        default=False,
        description="True when support orders this number by hand: it cannot be bought with POST /numbers. Ask support for it. Its quote_id still works for verification.",
    )


class CarrierOffer(QuoteOffer):
    """Internal offer: the public fields plus the carrier and its ids. Stored
    in ``number_offers.offer`` and read back by the purchase flow. Never
    returned by the public API (see ``QuoteOffer``)."""

    provider: Literal["twilio", "telnyx", "didww"] = Field(
        description="Carrier supplying this exact number."
    )
    verification_id: str | None = Field(
        default=None,
        description="Server-selected organization-bound approved bundle or requirement-group identifier, if any.",
    )
    address_id: str | None = Field(
        default=None,
        description="Server-selected verified address identifier, when supported; null otherwise.",
    )


def number_prefix(country: str, area_code: str) -> str:
    """E.164 start of every number in ``area_code``: ("GB", "20") -> "+4420"."""
    return f"+{phonenumbers.country_code_for_region(country)}{area_code}"


def cents(value) -> int:
    price = Decimal(str(value))
    if not price.is_finite() or price < 0:
        raise ValueError("Invalid carrier price")
    return int((price * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class NumberQuotesResponse(BaseModel):
    offers: list[QuoteOffer] = Field(
        description="Live offers ordered by readiness, remaining verification effort, monthly price and setup price."
    )
    recommended_quote_id: UUID | None = Field(
        description="Recommended ready offer, or null if none qualifies."
    )
    incomplete: bool = Field(
        description="True when some carriers did not answer, so the offers may not be complete. Search again later for a full comparison.",
    )
    expires_at: datetime = Field(
        description="UTC expiry of these persisted quotes; request fresh offers afterward."
    )
