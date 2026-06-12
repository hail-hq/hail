"""SMTP-listener inbound provider — placeholder.

The cloud-agnostic / OSS-only ingress path. Deferred to a follow-up
milestone (see docs/setup/smtp-inbound.md). The class exists so the
provider registry and tests have a stable identifier to import; every
method raises NotImplementedError.
"""

from __future__ import annotations

from collections.abc import Mapping

from hailhq.core.providers.email.inbound.base import (
    InboundMessage,
    InboundProvider,
)

__all__ = ["SmtpInboundProvider"]


class SmtpInboundProvider(InboundProvider):
    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        raise NotImplementedError(
            "SmtpInboundProvider is not yet implemented — "
            "see docs/setup/smtp-inbound.md"
        )

    async def parse_notification(self, body: bytes) -> InboundMessage:
        raise NotImplementedError(
            "SmtpInboundProvider is not yet implemented — "
            "see docs/setup/smtp-inbound.md"
        )
