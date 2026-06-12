"""Provider-neutral inbound email plumbing.

An ``InboundProvider`` accepts a raw notification (Lambda invoke for SES,
LMTP/SMTP for the future SMTP listener) and produces a single
``InboundMessage`` — the contract the rest of the pipeline consumes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from datetime import datetime

from pydantic import BaseModel

__all__ = ["InboundMessage", "InboundProvider"]


class InboundMessage(BaseModel):
    """One inbound mail event, normalized across providers."""

    provider_message_id: str
    envelope_from: str
    envelope_recipients: list[str]
    raw_s3_bucket: str
    raw_s3_key: str
    spam_verdict: str | None = None
    virus_verdict: str | None = None
    spf_verdict: str | None = None
    dkim_verdict: str | None = None
    dmarc_verdict: str | None = None
    received_at: datetime | None = None


class InboundProvider(ABC):
    """How raw MIME reaches Hail."""

    @abstractmethod
    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        """Confirm the notification originated from the configured provider."""

    @abstractmethod
    async def parse_notification(self, body: bytes) -> InboundMessage:
        """Decode the notification body into an ``InboundMessage``."""
