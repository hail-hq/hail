"""Carrier offer model shared by discovery adapters and the purchase flow."""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal
from uuid import UUID

from hailhq.core.schemas import NumberType
from pydantic import BaseModel, Field


class CarrierOffer(BaseModel):
    provider: Literal["twilio", "telnyx", "didww"] = Field(
        description="Carrier supplying this exact number."
    )
    e164: str = Field(description="Available phone number in E.164 format.")
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
    verification_id: str | None = Field(
        default=None,
        description="Server-selected organization-bound approved bundle or requirement-group identifier, if any.",
    )
    address_id: str | None = Field(
        default=None,
        description="Server-selected verified address identifier, when supported; null otherwise.",
    )
    quote_id: UUID | None = Field(
        default=None,
        description="Organization-bound quote identifier to pass to POST /numbers before expiry.",
    )


def cents(value) -> int:
    price = Decimal(str(value))
    if not price.is_finite() or price < 0:
        raise ValueError("Invalid carrier price")
    return int((price * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


class NumberQuotesResponse(BaseModel):
    offers: list[CarrierOffer] = Field(
        description="Live carrier offers ordered by readiness, remaining verification effort, monthly price and setup price. On a tie the first carrier listed wins."
    )
    recommended_quote_id: UUID | None = Field(
        description="Recommended ready offer matching the requested carrier preference, or null if none qualifies."
    )
    unavailable_providers: list[str] = Field(
        description="Carriers whose inventory, price, or regulatory lookup failed; comparison may be incomplete."
    )
    expires_at: datetime = Field(
        description="UTC expiry of these persisted quotes; request fresh offers afterward."
    )
