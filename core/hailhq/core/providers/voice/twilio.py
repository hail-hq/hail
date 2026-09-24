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

from hailhq.core.config import settings
from hailhq.core.providers.voice.base import (
    NumberNotProvisionable,
    NumberType,
    ProviderCallStatus,
    ProviderNumber,
    VoiceProvider,
)
from twilio.base.exceptions import TwilioRestException
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
        organization_id: str | None = None,
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
            purchased = await self._purchase(chosen.phone_number)
        except TwilioRestException as exc:
            # A 400 at purchase means the number can't be provisioned as
            # requested — most often a country/number-type that needs a
            # regulatory bundle (e.g. GB mobile: "Bundle required and not
            # provided"). If the organization has an approved bundle named
            # ``hail-<organization_id>``, retry once with it. Otherwise surface
            # a typed, non-retryable error the route maps to a 422, not an
            # opaque 500. Auth (401/403), rate-limit (429), and 5xx transport
            # failures propagate unchanged.
            if exc.status != 400:
                raise
            bundle_sid = None
            if organization_id:
                bundle_sid = await self._find_approved_bundle(
                    organization_id, country_code, number_type
                )
            if bundle_sid is None:
                raise NumberNotProvisionable(exc.msg) from exc
            try:
                purchased = await self._purchase(chosen.phone_number, bundle_sid)
            except TwilioRestException as retry_exc:
                if retry_exc.status == 400:
                    raise NumberNotProvisionable(retry_exc.msg) from retry_exc
                raise

        return ProviderNumber(
            provider_resource_id=purchased.sid,
            e164=purchased.phone_number,
            country_code=country_code,
            capabilities=_capabilities_to_list(purchased.capabilities),
            number_type=number_type,
        )

    async def _purchase(self, e164: str, bundle_sid: str | None = None):
        kwargs = {"bundle_sid": bundle_sid} if bundle_sid else {}
        return await asyncio.to_thread(
            self._client.incoming_phone_numbers.create,
            phone_number=e164,
            **kwargs,
        )

    async def _find_approved_bundle(
        self, organization_id: str, country_code: str, number_type: NumberType
    ) -> str | None:
        """SID of the organization's approved regulatory bundle for this
        country and number type, or None. The bundle is created by hand in the
        Twilio console with the friendly name ``hail-<organization_id>``."""
        name = f"hail-{organization_id}"
        try:
            bundles = await asyncio.to_thread(
                self._client.numbers.v2.regulatory_compliance.bundles.list,
                status="twilio-approved",
                friendly_name=name,
                iso_country=country_code,
                number_type=number_type.replace("_", "-"),
                limit=20,
            )
        except TwilioRestException:
            logger.exception("twilio bundle lookup failed for %s", name)
            return None
        return next((b.sid for b in bundles if b.friendly_name == name), None)

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
