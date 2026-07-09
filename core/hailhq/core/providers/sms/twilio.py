"""Twilio implementation of the carrier-side ``SmsProvider`` interface.

Same sync-SDK-wrapped-in-``asyncio.to_thread`` approach as
``providers/voice/twilio.py``; tests mock at the ``requests`` boundary via
``responses`` for the same reason (SDK drift shows up as a test failure).
"""

from __future__ import annotations

import asyncio

from twilio.base.exceptions import TwilioRestException
from twilio.rest import Client as TwilioClient

from hailhq.core.config import settings
from hailhq.core.providers.sms.base import ProviderSmsResult, SmsProvider


class TwilioSmsProvider(SmsProvider):
    """Carrier adapter for Twilio's Messages API."""

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
                    "TwilioSmsProvider requires twilio_account_sid + "
                    "twilio_auth_token (set them in settings or pass them "
                    "explicitly)."
                )
            client = TwilioClient(self.account_sid, token)
        self._client = client

    async def send_sms(
        self, from_e164: str, to_e164: str, body: str
    ) -> ProviderSmsResult:
        try:
            message = await asyncio.to_thread(
                self._client.messages.create,
                to=to_e164,
                from_=from_e164,
                body=body,
            )
        except TwilioRestException as exc:
            # A carrier/recipient-level rejection (invalid number, unsubscribed,
            # unverified, unreachable) is raised by the SDK at create time with
            # a 4xx status and a 21xxx error code and no message resource. Per
            # the SmsProvider contract that is returned as a failed result, not
            # raised. Account/auth/rate-limit (20xxx) and 5xx are transport
            # failures and propagate (the route maps them to a 502).
            code = exc.code or 0
            if not (exc.status and 400 <= exc.status < 500 and 21000 <= code < 22000):
                raise
            return ProviderSmsResult(
                provider_message_sid=None,
                status="failed",
                segment_count=0,
                error_code=str(exc.code),
            )
        raw_segments = getattr(message, "num_segments", None)
        segment_count = int(raw_segments) if raw_segments is not None else 1
        raw_error_code = getattr(message, "error_code", None)
        error_code = str(raw_error_code) if raw_error_code is not None else None
        return ProviderSmsResult(
            provider_message_sid=message.sid,
            status=message.status,
            segment_count=segment_count,
            error_code=error_code,
        )
