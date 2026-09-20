from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from hailhq.core.dns_lookup import (
    detect_dns_provider,
    dmarc_present,
    observe_record,
    resolve_mx,
    resolve_zone_ns,
    ses_inbound_host,
)


@pytest.fixture
def doh_client():
    """Patch httpx.AsyncClient the way the pre-existing resolve_mx tests do.

    Yields the mocked client instance so each test only has to wire up
    ``.get``.
    """
    with patch("hailhq.core.dns_lookup.httpx.AsyncClient") as client_cls:
        yield client_cls.return_value.__aenter__.return_value


def _fake_response(doh: dict) -> AsyncMock:
    fake = AsyncMock()
    fake.raise_for_status = lambda: None
    fake.json = lambda: doh
    return fake


def test_ses_inbound_host() -> None:
    assert ses_inbound_host("eu-west-1") == "inbound-smtp.eu-west-1.amazonaws.com"


# --------------------------------------------------------------------------- #
# resolve_mx — unchanged outward behaviour, rebuilt on _resolve
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_mx_parses_doh_answer(doh_client: AsyncMock) -> None:
    doh = {"Answer": [{"type": 15, "data": "10 inbound-smtp.eu-west-1.amazonaws.com."}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    hosts = await resolve_mx("inbox.acme.com")
    assert hosts == ["inbound-smtp.eu-west-1.amazonaws.com"]


@pytest.mark.asyncio
async def test_resolve_mx_empty_when_no_answer(doh_client: AsyncMock) -> None:
    doh_client.get = AsyncMock(return_value=_fake_response({"Status": 0}))
    assert await resolve_mx("acme.com") == []


@pytest.mark.asyncio
async def test_resolve_mx_raises_on_transport_error(doh_client: AsyncMock) -> None:
    """Existing callers (check_domain, verify) rely on resolve_mx raising —
    verify() catches Exception itself to degrade receive_ready to None."""
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(httpx.ConnectError):
        await resolve_mx("acme.com")


@pytest.mark.asyncio
async def test_resolve_mx_raises_on_http_status_error(doh_client: AsyncMock) -> None:
    fake = AsyncMock()
    fake.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "500", request=MagicMock(), response=MagicMock()
        )
    )
    doh_client.get = AsyncMock(return_value=fake)
    with pytest.raises(httpx.HTTPStatusError):
        await resolve_mx("acme.com")


# --------------------------------------------------------------------------- #
# resolve_zone_ns — strips left labels until an NS answer comes back
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_zone_ns_walks_up_to_the_apex(doh_client: AsyncMock) -> None:
    responses = {
        "inbox.mail.example.com": {"Answer": []},
        "mail.example.com": {"Answer": []},
        "example.com": {
            "Answer": [
                {"type": 2, "data": "ns1.example.com."},
                {"type": 2, "data": "NS2.example.com."},
            ]
        },
    }
    calls: list[str] = []

    async def fake_get(url: str, params: dict[str, str]) -> AsyncMock:
        calls.append(params["name"])
        return _fake_response(responses[params["name"]])

    doh_client.get = AsyncMock(side_effect=fake_get)
    zone, nameservers = await resolve_zone_ns("inbox.mail.example.com")
    assert zone == "example.com"
    assert nameservers == ["ns1.example.com", "ns2.example.com"]
    assert calls == ["inbox.mail.example.com", "mail.example.com", "example.com"]


@pytest.mark.asyncio
async def test_resolve_zone_ns_stops_before_querying_a_bare_tld(
    doh_client: AsyncMock,
) -> None:
    calls: list[str] = []

    async def fake_get(url: str, params: dict[str, str]) -> AsyncMock:
        calls.append(params["name"])
        return _fake_response({"Answer": []})

    doh_client.get = AsyncMock(side_effect=fake_get)
    zone, nameservers = await resolve_zone_ns("example.com")
    assert (zone, nameservers) == ("", [])
    assert "com" not in calls


@pytest.mark.asyncio
async def test_resolve_zone_ns_returns_empty_tuple_on_doh_error(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert await resolve_zone_ns("mail.example.com") == ("", [])


# --------------------------------------------------------------------------- #
# detect_dns_provider — suffix table from the A3 spec
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("provider_id", "nameserver"),
    [
        ("cloudflare", "ada.ns.cloudflare.com"),
        ("godaddy", "ns15.domaincontrol.com"),
        ("namecheap", "dns1.registrar-servers.com"),
        ("route53", "ns-123.awsdns-45.org"),
        ("google", "ns-cloud-a1.googledomains.com"),
        ("google", "ns1.google.com"),
        ("squarespace", "ns1.squarespacedns.com"),
        ("vercel", "ns1.vercel-dns.com"),
        ("digitalocean", "ns1.digitalocean.com"),
        ("ionos", "ns1067.ui-dns.com"),
        ("ionos", "ns1067.ui-dns.de"),
        ("hover", "ns1.hover.com"),
        ("namecom", "ns1.name.com"),
        ("porkbun", "curitiba.ns.porkbun.com"),
        ("gandi", "ns-6-a.gandi.net"),
        ("ovh", "dns17.ovh.net"),
    ],
)
def test_detect_dns_provider_matches_suffix(provider_id: str, nameserver: str) -> None:
    provider = detect_dns_provider([nameserver])
    assert provider is not None
    assert provider.id == provider_id


def test_detect_dns_provider_ignores_case_and_trailing_dot() -> None:
    provider = detect_dns_provider(["ADA.NS.CLOUDFLARE.COM."])
    assert provider is not None
    assert provider.id == "cloudflare"


def test_detect_dns_provider_unknown_nameservers_return_none() -> None:
    assert detect_dns_provider(["ns1.some-other-registrar.net"]) is None


def test_detect_dns_provider_cloudflare_has_proxy_note() -> None:
    provider = detect_dns_provider(["ada.ns.cloudflare.com"])
    assert provider is not None
    assert provider.note == "Set Proxy status to DNS only for every CNAME."


def test_detect_dns_provider_non_cloudflare_has_no_note() -> None:
    provider = detect_dns_provider(["ns15.domaincontrol.com"])
    assert provider is not None
    assert provider.note is None


# --------------------------------------------------------------------------- #
# observe_record — CNAME / MX / TXT, case + dot + quote insensitive
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_observe_record_cname_matches_ignoring_case_and_trailing_dot(
    doh_client: AsyncMock,
) -> None:
    doh = {"Answer": [{"type": 5, "data": "Target.Example.com."}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    observed = await observe_record(
        {
            "type": "CNAME",
            "name": "s1._domainkey.acme.com",
            "value": "target.example.com",
        }
    )
    assert observed is True


@pytest.mark.asyncio
async def test_observe_record_cname_no_match(doh_client: AsyncMock) -> None:
    doh = {"Answer": [{"type": 5, "data": "other.example.com."}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    observed = await observe_record(
        {
            "type": "CNAME",
            "name": "s1._domainkey.acme.com",
            "value": "target.example.com",
        }
    )
    assert observed is False


@pytest.mark.asyncio
async def test_observe_record_mx_matches_case_insensitively(
    doh_client: AsyncMock,
) -> None:
    doh = {
        "Answer": [{"type": 15, "data": "10 feedback-smtp.eu-west-1.amazonses.com."}]
    }
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    observed = await observe_record(
        {
            "type": "MX",
            "name": "send.acme.com",
            "value": "FEEDBACK-SMTP.eu-west-1.amazonses.com",
        }
    )
    assert observed is True


@pytest.mark.asyncio
async def test_observe_record_mx_no_match(doh_client: AsyncMock) -> None:
    doh = {"Answer": [{"type": 15, "data": "10 someone-else.amazonses.com."}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    observed = await observe_record(
        {
            "type": "MX",
            "name": "send.acme.com",
            "value": "feedback-smtp.eu-west-1.amazonses.com",
        }
    )
    assert observed is False


@pytest.mark.asyncio
async def test_observe_record_txt_matches_and_joins_split_chunks(
    doh_client: AsyncMock,
) -> None:
    doh = {"Answer": [{"type": 16, "data": '"v=spf1 " "include:amazonses.com ~all"'}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    observed = await observe_record(
        {
            "type": "TXT",
            "name": "send.acme.com",
            "value": "v=spf1 include:amazonses.com ~all",
        }
    )
    assert observed is True


@pytest.mark.asyncio
async def test_observe_record_txt_no_match(doh_client: AsyncMock) -> None:
    doh = {"Answer": [{"type": 16, "data": '"v=spf1 -all"'}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    observed = await observe_record(
        {
            "type": "TXT",
            "name": "send.acme.com",
            "value": "v=spf1 include:amazonses.com ~all",
        }
    )
    assert observed is False


@pytest.mark.asyncio
async def test_observe_record_returns_false_on_doh_error(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    observed = await observe_record(
        {
            "type": "CNAME",
            "name": "s1._domainkey.acme.com",
            "value": "target.example.com",
        }
    )
    assert observed is False


@pytest.mark.asyncio
async def test_observe_record_unknown_type_returns_false(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(return_value=_fake_response({"Answer": []}))
    observed = await observe_record(
        {"type": "AAAA", "name": "acme.com", "value": "::1"}
    )
    assert observed is False


# --------------------------------------------------------------------------- #
# dmarc_present
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_dmarc_present_true(doh_client: AsyncMock) -> None:
    doh = {"Answer": [{"type": 16, "data": '"v=DMARC1; p=none;"'}]}
    doh_client.get = AsyncMock(return_value=_fake_response(doh))
    assert await dmarc_present("acme.com") is True


@pytest.mark.asyncio
async def test_dmarc_present_false_when_absent(doh_client: AsyncMock) -> None:
    doh_client.get = AsyncMock(return_value=_fake_response({"Status": 3}))
    assert await dmarc_present("acme.com") is False


@pytest.mark.asyncio
async def test_dmarc_present_false_on_doh_error(doh_client: AsyncMock) -> None:
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    assert await dmarc_present("acme.com") is False
