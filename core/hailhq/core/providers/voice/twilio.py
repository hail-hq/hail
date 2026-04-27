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

from twilio.rest import Client as TwilioClient

from hailhq.core.config import settings
from hailhq.core.providers.voice.base import (
    NumberType,
    ProviderCallStatus,
    ProviderNumber,
    VoiceProvider,
)

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

        purchased = await asyncio.to_thread(
            self._client.incoming_phone_numbers.create,
            phone_number=chosen.phone_number,
        )

        return ProviderNumber(
            provider_resource_id=purchased.sid,
            e164=purchased.phone_number,
            country_code=country_code,
            capabilities=_capabilities_to_list(purchased.capabilities),
            number_type=number_type,
        )

    async def release_number(self, provider_resource_id: str) -> None:
        await asyncio.to_thread(
            self._client.incoming_phone_numbers(provider_resource_id).delete
        )

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
