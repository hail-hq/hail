"""SES-backed inbound provider.

Decodes the small JSON envelope our ses-ingest-lambda sends and
verifies the shared-secret HMAC. Raw MIME stays in S3; this adapter
does not fetch it — that's the ingest endpoint's job.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from datetime import datetime

from hailhq.core.providers.email.inbound.base import (
    InboundMessage,
    InboundProvider,
)

__all__ = ["SesInboundProvider"]


class SesInboundProvider(InboundProvider):
    def __init__(self, *, hmac_secret: str) -> None:
        if not hmac_secret:
            raise ValueError("SesInboundProvider requires a non-empty hmac_secret")
        self._secret = hmac_secret.encode()

    async def verify_notification(
        self, headers: Mapping[str, str], body: bytes
    ) -> bool:
        header = headers.get("X-Hail-Signature") or headers.get("x-hail-signature")
        if not header or not header.startswith("sha256="):
            return False
        provided = header.split("=", 1)[1]
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(provided.encode(), expected.encode())

    async def parse_notification(self, body: bytes) -> InboundMessage:
        data = json.loads(body)
        verdicts = data.get("verdicts") or {}
        ts = data.get("timestamp")
        received = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        return InboundMessage(
            provider_message_id=data["message_id"],
            envelope_from=data["envelope_from"],
            envelope_recipients=list(data["recipients"]),
            raw_s3_bucket=data["s3_bucket"],
            raw_s3_key=data["s3_key"],
            spam_verdict=verdicts.get("spam"),
            virus_verdict=verdicts.get("virus"),
            spf_verdict=verdicts.get("spf"),
            dkim_verdict=verdicts.get("dkim"),
            dmarc_verdict=verdicts.get("dmarc"),
            received_at=received,
        )
