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

from hailhq.core.carrier_offer import Approval, CarrierOffer, cents, number_prefix
from hailhq.core.config import settings
from hailhq.core.providers.verification.twilio import _TWILIO_TO_SUBJECT
from hailhq.core.providers.voice.base import (
    CarrierNotConfigured,
    CarrierRequestError,
    NumberType,
    OrderState,
    ProviderCallStatus,
    VoiceProvider,
)
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.http.http_client import TwilioHttpClient
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)


class TwilioVoiceProvider(VoiceProvider):
    """Carrier adapter for Twilio's REST API."""

    carrier = "twilio"

    def __init__(
        self,
        account_sid: str | None = None,
        auth_token: str | None = None,
        client: TwilioClient | None = None,
        timeout: float | None = None,
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
            client = TwilioClient(
                self.account_sid,
                token,
                http_client=(
                    TwilioHttpClient(timeout=timeout, max_retries=0)
                    if timeout is not None
                    else None
                ),
            )
        self._client = client

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

    async def get_call_error_code(self, provider_call_sid: str) -> str | None:
        """Call errors live on Notifications, not the Call resource itself."""
        notifications = await asyncio.to_thread(
            self._client.calls(provider_call_sid).notifications.list, log=0, limit=20
        )
        codes = [str(n.error_code) for n in notifications if n.error_code]
        return next(
            (code for code in codes if code == "32205"), codes[0] if codes else None
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


def _place_name(name: str | None) -> str | None:
    """Twilio names some places in capitals ("PHOENIX"); show them as the
    other carriers do ("Phoenix"). Mixed case stays as sent ("McAllen")."""
    if not name:
        return None
    return name.title() if name.isupper() else name


async def twilio_offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
    area_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    approvals: list[Approval] | None = None,
) -> list[CarrierOffer]:
    """Live Twilio inventory. ``approvals`` are the org's approved Hail
    verifications; one counts while its bundle is still approved at Twilio
    and was made for this country, type and the approval's subject type."""
    if not settings.twilio_account_sid or not settings.twilio_auth_token:
        return []
    approvals = approvals or []

    prefix = number_prefix(country, area_code) if area_code else None

    def discover():
        api = _order_client()
        inventory_api = getattr(api.available_phone_numbers(country), kind, None)
        if inventory_api is None:
            return []
        try:
            inventory = inventory_api.list(
                # "contains" may also match mid-number; a bigger page leaves
                # enough numbers that start with the prefix (DIDWW does the same).
                limit=30 if prefix else 3,
                **{f"{c}_enabled": True for c in capabilities},
                **({"contains": e164 or prefix} if e164 or prefix else {}),
                **({"in_locality": city} if city else {}),
                **({"in_region": region} if region else {}),
            )
        except TwilioRestException as exc:
            # Twilio answers 404 (20404) for a number type it does not sell in
            # this country. That is empty inventory, not a carrier outage.
            if exc.status == 404:
                return []
            raise
        except TwilioException as exc:
            # ``list()`` pages through ``Page.process_response``, which wraps a
            # non-200 page in a bare TwilioException with the response as the
            # second argument (not a TwilioRestException). Same 404 rule.
            response = exc.args[1] if len(exc.args) > 1 else None
            if getattr(response, "status_code", None) == 404:
                return []
            raise
        if e164:
            inventory = [n for n in inventory if n.phone_number == e164]
        if prefix:
            # "contains" may also match mid-number.
            inventory = [n for n in inventory if n.phone_number.startswith(prefix)]
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
        # Every end-user type: a person and a business each have their own
        # regulation. No hardcoded country exemptions.
        rules = compliance.regulations.list(
            iso_country=country,
            number_type=kind.replace("_", "-"),
            limit=100,
        )
        # Some countries return a regulation resource with no required fields
        # (US local is one). Presence of a resource alone is not a bundle gate.
        if any(not isinstance(r.requirements, dict) for r in rules):
            raise ValueError("Twilio regulatory requirements unavailable")
        rules = [
            r
            for r in rules
            if any(r.requirements.values()) and r.end_user_type in _TWILIO_TO_SUBJECT
        ]
        # Regulation sid -> the subject type it is for.
        subject_of = {r.sid: _TWILIO_TO_SUBJECT[r.end_user_type] for r in rules}
        subject_types = sorted(set(subject_of.values()))
        valid: list[Approval] = []
        wanted = [a for a in approvals if a.subject_type in subject_types]
        if wanted:
            bundles = compliance.bundles.list(
                status="twilio-approved", friendly_name=f"hail-{org}", limit=100
            )
            approved = {
                b.sid: subject_of.get(b.regulation_sid)
                for b in bundles
                if b.friendly_name == f"hail-{org}"
            }
            valid = [
                a
                for a in wanted
                if a.verification_id
                and approved.get(a.verification_id) == a.subject_type
            ]
        result = []
        for n in inventory:
            address_required = n.address_requirements not in (None, "none")
            needs_documents = bool(
                rules
                and not valid
                and any(r.requirements.get("supporting_document") for r in rules)
            )
            labels = (
                [
                    (
                        "Supporting documents and regulatory bundle"
                        if needs_documents
                        else "Identity or business information verification"
                    )
                ]
                if rules and not valid
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
                    subject_types=subject_types,
                    approvals=valid,
                    locality=_place_name(n.locality),
                    # Twilio sometimes puts the country here ("US"), not a state.
                    region=n.region if n.region and n.region != country else None,
                )
            )
        return result

    return await asyncio.to_thread(discover)


class LazyTwilioVoiceProvider(VoiceProvider):
    """Builds the Twilio client on first use, so a deployment without Twilio
    credentials can still serve numbers that belong to another carrier."""

    carrier = "twilio"

    def __init__(self) -> None:
        self._inner: TwilioVoiceProvider | None = None

    def _provider(self) -> TwilioVoiceProvider:
        if self._inner is None:
            self._inner = TwilioVoiceProvider()
        return self._inner

    async def release_number(self, provider_resource_id: str) -> None:
        await self._provider().release_number(provider_resource_id)

    async def get_call_status(self, provider_call_sid: str) -> ProviderCallStatus:
        return await self._provider().get_call_status(provider_call_sid)

    async def hangup_call(self, provider_call_sid: str) -> None:
        await self._provider().hangup_call(provider_call_sid)


async def release_twilio_number(resource_id: str) -> None:
    """Release an owned Twilio number. Raises ``CarrierNotConfigured`` if the
    credentials are missing."""
    try:
        provider = TwilioVoiceProvider()
    except ValueError as exc:
        raise CarrierNotConfigured(str(exc)) from exc
    await provider.release_number(resource_id)


# -- carrier interface (core/hailhq/core/carrier_routing.py) ---------------


async def offers(
    org: UUID,
    country: str,
    kind: NumberType,
    capabilities: list[str],
    e164: str | None = None,
    area_code: str | None = None,
    city: str | None = None,
    region: str | None = None,
    approvals: list[Approval] | None = None,
) -> list[CarrierOffer]:
    return await twilio_offers(
        org,
        country,
        kind,
        capabilities,
        e164=e164,
        area_code=area_code,
        city=city,
        region=region,
        approvals=approvals,
    )


async def place_order(number_id: UUID, offer: CarrierOffer) -> str:
    """Twilio buys at once: the result is the owned number's SID."""
    return await purchase_ordered_number(offer.e164, number_id, offer.verification_id)


async def order_outcome(
    e164: str, number_id: UUID, order_id: str | None, offer: CarrierOffer
) -> tuple[OrderState, str | None, str | None]:
    sid = await find_ordered_number(e164, number_id)
    return ("active", sid, None) if sid else ("missing", None, None)


async def release(resource_id: str) -> None:
    await release_twilio_number(resource_id)


# --- inbound: attach the number to the Elastic SIP trunk whose origination
# URI is LiveKit (docs/public/self-host/twilio.md). Trunk membership is what
# makes Twilio send the INVITE to LiveKit; without it the number rings out.


def _trunk_sid() -> str:
    if not settings.twilio_sip_trunk_sid:
        raise CarrierNotConfigured("TWILIO_SIP_TRUNK_SID is not set")
    return settings.twilio_sip_trunk_sid


async def attach_inbound_number(resource_id: str | None, e164: str) -> None:
    """Idempotent: a number already on the trunk is left alone."""
    if not resource_id:
        raise CarrierNotConfigured(f"{e164} has no Twilio phone number SID")
    trunk_sid = _trunk_sid()

    def attach() -> None:
        trunk = _order_client().trunking.v1.trunks(trunk_sid)
        try:
            trunk.phone_numbers(resource_id).fetch()
            return  # already attached
        except TwilioRestException as exc:
            if exc.status != 404:
                raise
        trunk.phone_numbers.create(phone_number_sid=resource_id)

    try:
        await asyncio.to_thread(attach)
    except TwilioRestException as exc:
        raise CarrierRequestError(exc.status) from exc


async def detach_inbound_number(resource_id: str | None, e164: str) -> None:
    """Idempotent: a number not on the trunk (or already released) is fine."""
    if not resource_id:
        return
    trunk_sid = _trunk_sid()

    def detach() -> None:
        try:
            _order_client().trunking.v1.trunks(trunk_sid).phone_numbers(
                resource_id
            ).delete()
        except TwilioRestException as exc:
            if exc.status != 404:
                raise

    try:
        await asyncio.to_thread(detach)
    except TwilioRestException as exc:
        raise CarrierRequestError(exc.status) from exc
