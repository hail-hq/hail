from unittest.mock import AsyncMock, patch

import pytest
from hailhq.core.dns_lookup import resolve_mx, ses_inbound_host


def test_ses_inbound_host() -> None:
    assert ses_inbound_host("eu-west-1") == "inbound-smtp.eu-west-1.amazonaws.com"


@pytest.mark.asyncio
async def test_resolve_mx_parses_doh_answer() -> None:
    doh = {"Answer": [{"type": 15, "data": "10 inbound-smtp.eu-west-1.amazonaws.com."}]}
    fake = AsyncMock()
    fake.json = lambda: doh
    fake.raise_for_status = lambda: None
    with patch("hailhq.core.dns_lookup.httpx.AsyncClient") as client_cls:
        instance = client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        hosts = await resolve_mx("inbox.acme.com")
    assert hosts == ["inbound-smtp.eu-west-1.amazonaws.com"]


@pytest.mark.asyncio
async def test_resolve_mx_empty_when_no_answer() -> None:
    fake = AsyncMock()
    fake.json = lambda: {"Status": 0}
    fake.raise_for_status = lambda: None
    with patch("hailhq.core.dns_lookup.httpx.AsyncClient") as client_cls:
        instance = client_cls.return_value.__aenter__.return_value
        instance.get = AsyncMock(return_value=fake)
        assert await resolve_mx("acme.com") == []
