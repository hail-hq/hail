"""Twilio implementation of the carrier-side ``SmsProvider`` interface.

Same sync-SDK-wrapped-in-``asyncio.to_thread`` approach as
``providers/voice/twilio.py``; tests mock at the ``requests`` boundary via
``responses`` for the same reason (SDK drift shows up as a test failure).
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.providers.sms.base import (
    ProviderSmsResult,
    SmsProvider,
    SmsProvisioningError,
)
from hailhq.core.urls import join_url
from twilio.base.exceptions import TwilioException, TwilioRestException
from twilio.rest import Client as TwilioClient

logger = logging.getLogger(__name__)

# Twilio error 21712: the number sits in another messaging service.
_NUMBER_IN_ANOTHER_SERVICE = 21712
# Twilio error 21710: the number is already in this messaging service.
_NUMBER_ALREADY_IN_SERVICE = 21710

# A "yes" from the toll-free approval check is kept this long per process, so
# a bulk send does not ask Twilio before every text. A "no" is never kept.
_TOLLFREE_APPROVED_TTL_SECONDS = 600
# (account SID, number SID, E.164) -> monotonic time the "yes" expires.
_tollfree_approved_until: dict[tuple[str, str, str], float] = {}


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

    async def tollfree_approved(self, *, provider_resource_id: str, e164: str) -> bool:
        """Preserve existing manual approvals without importing identity or filing.

        The current owned phone and every TFV record read must match this
        account and exact SID, and the newest record must be approved. An old
        approval for another/released number never authorizes this sender.
        A "yes" is cached for ``_TOLLFREE_APPROVED_TTL_SECONDS``.
        """
        key = (self.account_sid or "", provider_resource_id, e164)
        if _tollfree_approved_until.get(key, 0.0) > time.monotonic():
            return True

        def read():
            phone = self._client.incoming_phone_numbers(provider_resource_id).fetch()
            if (
                phone.sid != provider_resource_id
                or phone.phone_number != e164
                or phone.account_sid != self.account_sid
            ):
                return False
            records = self._client.messaging.v1.tollfree_verifications.list(
                # Every page: the list order is not documented, and the
                # newest record decides.
                tollfree_phone_number_sid=provider_resource_id,
                # One page holds every record of a number in practice, so the
                # read stays inside the 5 second wait below.
                page_size=100,
            )
            if not records or any(
                r.tollfree_phone_number_sid != provider_resource_id
                or r.account_sid != self.account_sid
                for r in records
            ):
                return False
            oldest = datetime.min.replace(tzinfo=timezone.utc)
            newest = max(records, key=lambda r: r.date_created or oldest)
            return newest.status == "TWILIO_APPROVED"

        try:
            approved = await asyncio.wait_for(asyncio.to_thread(read), timeout=5)
        except Exception as exc:
            logger.warning(
                "Toll-free approval lookup failed: exception=%s status=%s code=%s",
                type(exc).__name__,
                exc.status if isinstance(exc, TwilioRestException) else None,
                exc.code if isinstance(exc, TwilioRestException) else None,
            )
            return False
        if approved:
            now = time.monotonic()
            # Drop expired entries so numbers no longer texted do not pile up.
            for stale in [k for k, t in _tollfree_approved_until.items() if t <= now]:
                del _tollfree_approved_until[stale]
            _tollfree_approved_until[key] = now + _TOLLFREE_APPROVED_TTL_SECONDS
        else:
            _tollfree_approved_until.pop(key, None)
        return approved

    async def send_sms(
        self,
        from_e164: str,
        to_e164: str,
        body: str,
        status_callback_url: str | None = None,
        messaging_service_sid: str | None = None,
    ) -> ProviderSmsResult:
        # An alphanumeric ``From`` sends on its own; no messaging service needed.
        create_kwargs: dict[str, str] = {
            "to": to_e164,
            "from_": from_e164,
            "body": body,
        }
        if status_callback_url is not None:
            create_kwargs["status_callback"] = status_callback_url
        try:
            message = await asyncio.to_thread(
                self._client.messages.create,
                **create_kwargs,
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

    async def ensure_messaging_service(
        self,
        organization_id: UUID,
        existing_sid: str | None,
        name: str | None = None,
    ) -> str:
        """``name`` picks a service other than the organization's own (a
        person's US texting campaign has one of its own)."""
        # Inbound texts to every number in the service reach Hail here; the
        # number's own "A Message Comes In" webhook is not used. An existing
        # service gets the same URL, so one created before this setting (or
        # one whose HAIL_API_URL changed) still delivers.
        inbound = {
            "inbound_request_url": join_url(settings.hail_api_url, "sms/inbound"),
            "inbound_method": "POST",
            "use_inbound_webhook_on_number": False,
        }
        try:
            if existing_sid is not None:
                await asyncio.to_thread(
                    self._client.messaging.v1.services(existing_sid).update, **inbound
                )
                return existing_sid
            # A first attach that failed left a service no row records.
            # Find it by name so a retry reuses it instead of adding another.
            name = name or f"hail-org-{organization_id}"
            found = await asyncio.to_thread(self._find_service_sid_by_name, name)
            if found is not None:
                await asyncio.to_thread(
                    self._client.messaging.v1.services(found).update, **inbound
                )
                return found
            service = await asyncio.to_thread(
                self._client.messaging.v1.services.create,
                friendly_name=name,
                **inbound,
            )
        except TwilioRestException as exc:
            raise SmsProvisioningError(exc.msg or str(exc)) from exc
        return service.sid

    async def attach_number(
        self, messaging_service_sid: str, provider_resource_id: str
    ) -> None:
        try:
            await self._attach(messaging_service_sid, provider_resource_id)
        except TwilioRestException as exc:
            if exc.code == _NUMBER_ALREADY_IN_SERVICE:
                return  # Retry after a success: nothing to do.
            if exc.code != _NUMBER_IN_ANOTHER_SERVICE:
                # A refused attach (unknown number sid, account restriction)
                # is the carrier's answer, not a server fault: the route
                # turns it into a 502.
                raise SmsProvisioningError(exc.msg or str(exc)) from exc
        else:
            return
        # Twilio keeps a number in one messaging service at a time. The
        # account is Hail's and the number is this org's, so a number another
        # service holds (Twilio's own "Default Messaging Service for
        # Conversations", a service of a released row) is moved to ours.
        try:
            holder = await asyncio.to_thread(
                self._service_holding, provider_resource_id
            )
        except TwilioException as exc:
            # List calls raise the base class, not TwilioRestException.
            raise SmsProvisioningError(str(exc)) from exc
        if holder == messaging_service_sid:
            return  # Already ours: nothing to do.
        if holder is None:
            raise SmsProvisioningError(
                f"{provider_resource_id} is in another messaging service that this "
                "account cannot see"
            )
        logger.info(
            "moving %s from messaging service %s to %s",
            provider_resource_id,
            holder,
            messaging_service_sid,
        )
        try:
            await asyncio.to_thread(
                self._client.messaging.v1.services(holder)
                .phone_numbers(provider_resource_id)
                .delete
            )
        except TwilioRestException as exc:
            raise SmsProvisioningError(exc.msg or str(exc)) from exc
        try:
            await self._attach(messaging_service_sid, provider_resource_id)
        except TwilioRestException as exc:
            # Put the number back so the old service is not left without it.
            try:
                await self._attach(holder, provider_resource_id)
            except TwilioRestException:
                logger.error(
                    "could not restore %s to messaging service %s",
                    provider_resource_id,
                    holder,
                )
            raise SmsProvisioningError(exc.msg or str(exc)) from exc

    async def _attach(
        self, messaging_service_sid: str, provider_resource_id: str
    ) -> None:
        await asyncio.to_thread(
            self._client.messaging.v1.services(
                messaging_service_sid
            ).phone_numbers.create,
            phone_number_sid=provider_resource_id,
        )

    def _find_service_sid_by_name(self, friendly_name: str) -> str | None:
        for service in self._client.messaging.v1.services.stream():
            if service.friendly_name == friendly_name:
                return service.sid
        return None

    def _service_holding(self, provider_resource_id: str) -> str | None:
        """The sid of the messaging service that holds this number, if any.
        Twilio has no lookup by number, so every service is scanned."""
        for service in self._client.messaging.v1.services.stream():
            numbers = self._client.messaging.v1.services(service.sid).phone_numbers
            if any(n.sid == provider_resource_id for n in numbers.stream()):
                return service.sid
        return None


class LazyTwilioSmsProvider(SmsProvider):
    """Builds the Twilio client on first use, so a deployment without Twilio
    credentials can still serve numbers that belong to another carrier."""

    def __init__(self) -> None:
        self._inner: TwilioSmsProvider | None = None

    def _provider(self) -> TwilioSmsProvider:
        if self._inner is None:
            self._inner = TwilioSmsProvider()
        return self._inner

    async def tollfree_approved(self, *, provider_resource_id: str, e164: str) -> bool:
        return await self._provider().tollfree_approved(
            provider_resource_id=provider_resource_id, e164=e164
        )

    async def send_sms(
        self,
        from_e164: str,
        to_e164: str,
        body: str,
        status_callback_url: str | None = None,
        messaging_service_sid: str | None = None,
    ) -> ProviderSmsResult:
        return await self._provider().send_sms(
            from_e164, to_e164, body, status_callback_url, messaging_service_sid
        )

    async def ensure_messaging_service(
        self,
        organization_id: UUID,
        existing_sid: str | None,
        name: str | None = None,
    ) -> str:
        """``name`` picks a service other than the organization's own (a
        person's US texting campaign has one of its own)."""
        return await self._provider().ensure_messaging_service(
            organization_id, existing_sid
        )

    async def attach_number(
        self, messaging_service_sid: str, provider_resource_id: str
    ) -> None:
        await self._provider().attach_number(
            messaging_service_sid, provider_resource_id
        )


_default: LazyTwilioSmsProvider | None = None


def twilio_sms_provider() -> SmsProvider:
    """The process-wide Twilio SMS provider. Its client is built on first
    use, so a deployment without Twilio credentials still starts."""
    global _default
    if _default is None:
        _default = LazyTwilioSmsProvider()
    return _default
