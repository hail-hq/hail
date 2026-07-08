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

from pydantic import BaseModel

__all__ = ["ProviderSmsResult", "SmsProvider"]


class ProviderSmsResult(BaseModel):
    """The carrier's immediate response to a send request."""

    provider_message_sid: str
    status: str
    segment_count: int
    error_code: str | None = None


class SmsProvider(ABC):
    """Abstract carrier-side SMS provider."""

    @abstractmethod
    async def send_sms(
        self, from_e164: str, to_e164: str, body: str
    ) -> ProviderSmsResult:
        """Send a single SMS.

        Raises on transport/auth/connection failure. A carrier-level
        rejection (e.g. an invalid number) is not raised — it is returned
        as a ``ProviderSmsResult`` with ``error_code`` populated and
        ``status`` reflecting the failure.
        """
