"""Email provider interface.

An ``EmailProvider`` covers two surfaces:

* **Sending** — push a message to one or more recipients and get back a
  provider-native message id.
* **Identity management** — create, inspect, and delete the sending-domain
  identity required before the provider accepts mail with that From-address.

Splitting these the way the voice interface splits "carrier" from "media"
keeps the SES swap-out story honest: a future Resend/Postmark adapter
implements the same protocol, and the API layer doesn't reach for SDK
specifics.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Literal

from pydantic import BaseModel

__all__ = [
    "DkimRecord",
    "EmailProvider",
    "IdentityVerificationStatus",
    "ProviderIdentity",
    "ProviderSendResult",
]


IdentityVerificationStatus = Literal["pending", "verified", "failed"]


class DkimRecord(BaseModel):
    """One DNS CNAME the operator must publish before the domain is usable.

    SES returns three of these (selectors 1/2/3); the API surfaces them
    verbatim so the caller can copy them straight into their DNS console.
    """

    name: str
    value: str
    type: Literal["CNAME"] = "CNAME"


class ProviderIdentity(BaseModel):
    """Provider's view of a sender domain identity."""

    domain: str
    verification_status: IdentityVerificationStatus
    dkim_records: list[DkimRecord]
    # MAIL FROM domain (used for the SPF Return-Path); SES returns this only
    # for custom domains once the operator configures one. Hail-mail rows
    # skip it because the parent domain is pre-configured by the operator.
    mail_from_domain: str | None = None
    # Provider-native identity ARN/id — opaque, stored for delete + audit.
    provider_resource_id: str | None = None


class ProviderSendResult(BaseModel):
    """Successful send outcome.

    Failures raise rather than return a result — the call site turns the
    exception into a status='failed' row.
    """

    provider_message_id: str


class EmailProvider(ABC):
    """Abstract email provider."""

    @abstractmethod
    async def send_email(
        self,
        *,
        from_address: str,
        to_addresses: list[str],
        subject: str,
        body_text: str | None,
        body_html: str | None,
        cc: list[str] | None = None,
        bcc: list[str] | None = None,
        reply_to: str | None = None,
    ) -> ProviderSendResult:
        """Send one message. Implementations must raise on provider error."""

    @abstractmethod
    async def create_identity(self, domain: str) -> ProviderIdentity:
        """Register a sending domain and return its DKIM publication records."""

    @abstractmethod
    async def get_identity(self, domain: str) -> ProviderIdentity:
        """Fetch the provider's current view of a domain identity."""

    @abstractmethod
    async def delete_identity(self, domain: str) -> None:
        """Remove a sending domain. Idempotent on missing identities."""
