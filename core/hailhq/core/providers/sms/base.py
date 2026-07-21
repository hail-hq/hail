"""Carrier-side SMS provider interface.

Unlike ``VoiceProvider`` (see ``providers/voice/base.py``), SMS has no
in-flight state to poll: a send either succeeds (accepted/queued at the
carrier) or fails. Only transport/auth/connection failures raise. A
carrier-level rejection (e.g. an invalid number) is not raised — it comes
back as a normal ``ProviderSmsResult`` with ``error_code`` populated and
``status`` reflecting the failure (e.g. ``"failed"``). Later status
transitions (delivered/undelivered) arrive via a webhook, not polling —
that's a later phase's concern, not this interface's.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from uuid import UUID

from pydantic import BaseModel

__all__ = ["ProviderSmsResult", "SmsProvider"]


class ProviderSmsResult(BaseModel):
    """The carrier's immediate response to a send request."""

    # ``None`` when the carrier rejected the send at create time (no message
    # resource was created — e.g. an invalid number); populated once the
    # carrier accepts and mints a message id.
    provider_message_sid: str | None = None
    status: str
    segment_count: int
    error_code: str | None = None


class SmsProvider(ABC):
    """Abstract carrier-side SMS provider."""

    @abstractmethod
    async def send_sms(
        self,
        from_e164: str,
        to_e164: str,
        body: str,
        status_callback_url: str | None = None,
    ) -> ProviderSmsResult:
        """Send a single SMS.

        Raises on transport/auth/connection failure. A carrier-level
        rejection (e.g. an invalid number) is not raised — it is returned
        as a ``ProviderSmsResult`` with ``error_code`` populated and
        ``status`` reflecting the failure.

        ``status_callback_url``, when set, asks the carrier to POST
        delivery-status updates (e.g. delivered/undelivered) to that URL
        as the message transitions after the initial accept.
        """

    @abstractmethod
    async def ensure_messaging_service(
        self, organization_id: UUID, existing_sid: str | None
    ) -> str:
        """Return a Messaging Service SID for this org — the existing one
        if provided, otherwise create a new one and return its SID."""

    @abstractmethod
    async def attach_number(
        self, messaging_service_sid: str, provider_resource_id: str
    ) -> None:
        """Attach a purchased phone number (by its Twilio SID) to a
        Messaging Service's sender pool."""
