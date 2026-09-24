"""Twilio implementation of the carrier-side ``VoiceProvider`` interface.

The Twilio Python SDK is sync-only and uses ``requests`` for transport
under the hood. We wrap each individual SDK call in
``asyncio.to_thread`` so this adapter exposes ``async def`` methods to
FastAPI handlers without blocking the event loop. Tests mock at the
``requests`` boundary via ``responses`` so SDK API drift surfaces as
test failures rather than silent breakage.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from hailhq.core.carrier_offer import CarrierOffer, cents
from hailhq.core.config import settings
from hailhq.core.providers.voice.base import (
    CarrierRequestError,
    NumberNotProvisionable,
    NumberType,
    ProviderCallStatus,
    ProviderNumber,
    VoiceProvider,
)
from twilio.base.exceptions import TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

# Maps the Hail-canonical capability strings to Twilio's available-number
# search kwargs (which take booleans). Anything in `capabilities` that
# isn't listed here is silently ignored — the search just won't filter on
# it, and real coverage is reported back from the purchased number's
# capabilities dict.
_CAPABILITY_TO_SEARCH_KWARG = {
    "voice": "voice_enabled",
    "sms": "sms_enabled",
    "mms": "mms_enabled",
    "fax": "fax_enabled",
}


def _capabilities_to_list(caps: dict[str, bool] | None) -> list[str]:
    """Normalize Twilio's capabilities dict ``{"voice": True, "SMS": True}``
    into a sorted lowercase string list ``["sms", "voice"]``.
    """
    return sorted(k.lower() for k, v in (caps or {}).items() if v)


class TwilioVoiceProvider(VoiceProvider):
    """Carrier adapter for Twilio's REST API."""

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        client: TwilioClient | None = None,
    ) -> None:
        self.account_sid = account_sid or settings.twilio_account_sid
        token = auth_token or settings.twilio_auth_token

        if client is None:
            if not self.account_sid or not token:
                raise ValueError(
                    "TwilioVoiceProvider requires twilio_account_sid + "
                    "twilio_auth_token (set them in settings or pass them "
                    "explicitly)."
                )
            client = TwilioClient(self.account_sid, token)
        self._client = client

    async def acquire_number(
        self,
        country_code: str,
        number_type: NumberType,
        capabilities: list[str],
    ) -> ProviderNumber:
        search_kwargs: dict[str, bool] = {}
        for cap in capabilities:
            kw = _CAPABILITY_TO_SEARCH_KWARG.get(cap.lower())
            if kw is not None:
                search_kwargs[kw] = True

        country_ctx = self._client.available_phone_numbers(country_code)
        list_ctx = getattr(country_ctx, number_type)

        available = await asyncio.to_thread(list_ctx.list, limit=1, **search_kwargs)
        if not available:
            raise LookupError(
                f"No {number_type} numbers available in {country_code} matching "
                f"capabilities={capabilities}."
            )
        chosen = available[0]

        try:
            purchased = await asyncio.to_thread(
                self._client.incoming_phone_numbers.create,
                phone_number=chosen.phone_number,
            )
        except TwilioRestException as exc:
            # A 400 at purchase means the number can't be provisioned as
            # requested — most often a country/number-type that needs a
            # regulatory bundle or address we haven't set up (e.g. GB mobile:
            # "Bundle required and not provided"). Surface it as a typed,
            # non-retryable error the route maps to a 422, not an opaque 500.
            # Auth (401/403), rate-limit (429), and 5xx transport failures
            # propagate unchanged.
            if exc.status == 400:
                raise NumberNotProvisionable(exc.msg) from exc
            raise

        return ProviderNumber(
            provider_resource_id=purchased.sid,
            e164=purchased.phone_number,
            country_code=country_code,
            capabilities=_capabilities_to_list(purchased.capabilities),
            number_type=number_type,
        )

    async def release_number(self, provider_resource_id: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.incoming_phone_numbers(provider_resource_id).delete
            )
        except TwilioRestException as exc:
            # Already gone at the carrier (released out of band, or a retry of
            # a half-completed release): the desired end state holds. Loud,
            # not silent — a 404 is also what wrong credentials (a different
            # (sub)account) or a stale/mangled SID produce, and in that case
            # the number is still live and billed at Twilio while we mark the
            # row released.
            if exc.status == 404:
                logger.warning(
                    "twilio release of %s returned 404; treating as already "
                    "released — if this number was expected to be live, check "
                    "the account credentials and the stored SID",
                    provider_resource_id,
                )
                return
            raise

    async def get_call_status(self, provider_call_sid: str) -> ProviderCallStatus:
        call = await asyncio.to_thread(self._client.calls(provider_call_sid).fetch)

        # Twilio's REST `Call` resource has no first-class "answered"
        # timestamp. `start_time` is when Twilio originated the call,
        # which is the closest signal available here. Real per-leg
        # answer events come in via webhooks (out of scope for this
        # adapter).
        answered_at = getattr(call, "start_time", None)
        ended_at = getattr(call, "end_time", None)
        raw_duration = getattr(call, "duration", None)
        duration_seconds = int(raw_duration) if raw_duration is not None else None

        return ProviderCallStatus(
            provider_call_sid=call.sid,
            status=call.status,
            answered_at=answered_at,
            ended_at=ended_at,
            duration_seconds=duration_seconds,
        )

    async def hangup_call(self, provider_call_sid: str) -> None:
        await asyncio.to_thread(
            self._client.calls(provider_call_sid).update, status="completed"
        )


def _order_client() -> TwilioClient:
    """Short timeout, no retries: a paid purchase must never be repeated."""
    return TwilioClient(
        settings.twilio_account_sid,
        settings.twilio_auth_token,
        http_client=TwilioHttpClient(timeout=10, max_retries=0),
    )


def _order_name(order_ref: UUID) -> str:
    return f"hail-order-{order_ref}"


async def purchase_ordered_number(
    e164: str, order_ref: UUID, bundle_sid: str | None
) -> str:
    """Buy one exact number once; returns its SID. The friendly name lets
    ``find_ordered_number`` recover an outcome the response never delivered."""

    def create() -> str:
        kwargs = {"phone_number": e164, "friendly_name": _order_name(order_ref)}
        if bundle_sid:
            kwargs["bundle_sid"] = bundle_sid
        return _order_client().incoming_phone_numbers.create(**kwargs).sid

    try:
        return await asyncio.to_thread(create)
    except TwilioRestException as exc:
        raise CarrierRequestError(exc.status) from exc


async def find_ordered_number(e164: str, order_ref: UUID) -> str | None:
    """SID of the number bought for ``order_ref``, or None if none was bought."""

    def lookup() -> str | None:
        found = _order_client().incoming_phone_numbers.list(phone_number=e164, limit=10)
        return next(
            (
                n.sid
                for n in found
                if n.phone_number == e164 and n.friendly_name == _order_name(order_ref)
            ),
            None,
        )

    return await asyncio.to_thread(lookup)


async def twilio_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
) -> list[CarrierOffer]:
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        return []

    def discover():
        api = _order_client()
        inventory_api = getattr(api.available_phone_numbers(country), kind, None)
        if inventory_api is None:
            return []
        try:
            inventory = inventory_api.list(
                limit=3,
                **{f"{c}_enabled": True for c in capabilities},
                **({"contains": e164} if e164 else {}),
            )
        except TwilioRestException as exc:
            # Twilio answers 404 (20404) for a number type it does not sell in
            # this country. That is empty inventory, not a carrier outage.
            if exc.status == 404:
                return []
            raise
        if e164:
            inventory = [n for n in inventory if n.phone_number == e164]
        if not inventory:
            return []
        pricing = api.pricing.v1.phone_numbers.countries(country).fetch()
        if (pricing.price_unit or "").upper() != "USD":
            return []
        prices = [
            p
            for p in (pricing.phone_number_prices or [])
            if p["number_type"].replace("-", "_").replace(" ", "_") == kind
        ]
        if not prices:
            return []
        monthly_cents = cents(prices[0]["current_price"])
        if monthly_cents <= 0:
            return []
        compliance = api.numbers.v2.regulatory_compliance
        # Organizations are business end users. No hardcoded country exemptions.
        rules = compliance.regulations.list(
            iso_country=country,
            number_type=kind.replace("_", "-"),
            end_user_type="business",
            limit=100,
        )
        # Some countries return a regulation resource with no required fields
        # (US local is one). Presence of a resource alone is not a bundle gate.
        if any(not isinstance(r.requirements, dict) for r in rules):
            raise ValueError("Twilio regulatory requirements unavailable")
        rules = [r for r in rules if any(r.requirements.values())]
        bundle = None
        if rules:
            bundles = compliance.bundles.list(
                status="twilio-approved", friendly_name=f"hail-{org}", limit=100
            )
            bundle = next(
                (
                    b
                    for b in bundles
                    if b.friendly_name == f"hail-{org}"
                    and b.regulation_sid in {r.sid for r in rules}
                ),
                None,
            )
        result = []
        for n in inventory:
            address_required = n.address_requirements not in (None, "none")
            needs_documents = bool(
                rules
                and not bundle
                and any(r.requirements.get("supporting_document") for r in rules)
            )
            labels = (
                [
                    (
                        "Supporting documents and regulatory bundle"
                        if needs_documents
                        else "Business information verification"
                    )
                ]
                if rules and not bundle
                else []
            )
            if address_required:
                labels.append("Verified address for this number")
            result.append(
                CarrierOffer(
                    provider="twilio",
                    e164=n.phone_number,
                    country_code=country,
                    number_type=kind,
                    capabilities=sorted(
                        k.lower()
                        for k, v in n.capabilities.items()
                        if v and k.lower() in {"voice", "sms"}
                    ),
                    monthly_cents=monthly_cents,
                    setup_cents=0,
                    readiness="verification_required" if labels else "ready",
                    regulatory_friction=(
                        "none"
                        if not labels
                        else "documents" if needs_documents else "information"
                    ),
                    requirements=labels,
                    verification_id=bundle.sid if bundle else None,
                )
            )
        return result

    return await asyncio.to_thread(discover)
