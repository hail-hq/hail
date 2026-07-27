import asyncio

import pytest
from hailhq.core.providers.email.inbound.smtp import SmtpInboundProvider


def test_smtp_provider_raises_not_implemented():
    p = SmtpInboundProvider()
    with pytest.raises(NotImplementedError):
        asyncio.run(p.verify_notification({}, b""))
    with pytest.raises(NotImplementedError):
        asyncio.run(p.parse_notification(b""))
