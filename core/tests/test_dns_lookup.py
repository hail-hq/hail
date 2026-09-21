from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from hailhq.core.dns_lookup import (
    DnsLookupError,
    detect_dns_provider,
    dmarc_present,
    observe_record,
    organizational_domain,
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


@pytest.mark.parametrize(
    ("domain", "expected"),
    [
        ("acme.com", "acme.com"),
        ("A.Mail.Acme.COM.", "acme.com"),
        ("mail.acme.co.uk", "acme.co.uk"),
        # Private suffixes count: a customer on a shared host holds only
        # their own label, never vercel.app / github.io.
        ("foo.vercel.app", "foo.vercel.app"),
        ("x.github.io", "x.github.io"),
        # A public suffix, or a name under none, has no organizational domain.
        ("co.uk", ""),
        ("vercel.app", ""),
        ("localhost", ""),
    ],
)
def test_organizational_domain(domain: str, expected: str) -> None:
    assert organizational_domain(domain) == expected


@pytest.mark.parametrize(
    ("domain", "expected_calls"),
    [
        # acme.co.uk is not delegated; co.uk HAS NS records of its own, so
        # querying it would report the registry as the customer's zone.
        ("mail.acme.co.uk", ["mail.acme.co.uk", "acme.co.uk"]),
        ("foo.vercel.app", ["foo.vercel.app"]),
    ],
)
@pytest.mark.asyncio
async def test_resolve_zone_ns_never_climbs_into_a_public_suffix(
    doh_client: AsyncMock, domain: str, expected_calls: list[str]
) -> None:
    calls: list[str] = []

    async def fake_get(url: str, params: dict[str, str]) -> AsyncMock:
        calls.append(params["name"])
        if params["name"] in ("co.uk", "vercel.app"):
            return _fake_response({"Answer": [{"type": 2, "data": "ns1.nic.uk."}]})
        return _fake_response({"Answer": []})

    doh_client.get = AsyncMock(side_effect=fake_get)
    assert await resolve_zone_ns(domain) == ("", [])
    assert calls == expected_calls


@pytest.mark.asyncio
async def test_resolve_zone_ns_public_suffix_itself_does_no_lookups(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(side_effect=AssertionError("must not query"))
    assert await resolve_zone_ns("co.uk") == ("", [])


@pytest.mark.asyncio
async def test_helpers_use_a_passed_client_and_open_none_of_their_own() -> None:
    """The dns-check route passes one client to every helper so its ~8
    lookups share a TLS connection."""
    shared = AsyncMock()
    shared.get = AsyncMock(
        return_value=_fake_response(
            {"Answer": [{"type": 16, "data": '"v=DMARC1; p=none;"'}]}
        )
    )
    with patch("hailhq.core.dns_lookup.httpx.AsyncClient") as client_cls:
        assert await dmarc_present("acme.com", client=shared) is True
        await observe_record(
            {"type": "TXT", "name": "acme.com", "value": "x"}, client=shared
        )
        await resolve_zone_ns("acme.com", client=shared)
    client_cls.assert_not_called()
    assert shared.get.await_count == 3


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
async def test_resolve_zone_ns_raises_dns_lookup_error_on_doh_failure(
    doh_client: AsyncMock,
) -> None:
    """A lookup failure is not "no answer" — it must not resolve to a zone."""
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DnsLookupError):
        await resolve_zone_ns("mail.example.com")


@pytest.mark.asyncio
async def test_resolve_zone_ns_aborts_on_first_lookup_failure_without_climbing(
    doh_client: AsyncMock,
) -> None:
    """A transient DoH failure on the first NS query must abort the walk,
    not be treated as "no answer" and retried one label up. Regression for:
    a failed NS query on acme.co.uk must never fall through to co.uk."""
    calls: list[str] = []

    async def fake_get(url: str, params: dict[str, str]) -> AsyncMock:
        calls.append(params["name"])
        raise httpx.ConnectError("boom")

    doh_client.get = AsyncMock(side_effect=fake_get)
    with pytest.raises(DnsLookupError):
        await resolve_zone_ns("acme.co.uk")
    assert calls == ["acme.co.uk"]


# --------------------------------------------------------------------------- #
# resolve_zone_ns / observe_record — a DoH ``Status`` other than NOERROR (0)
# or NXDOMAIN (3), e.g. SERVFAIL (2), is HTTP 200 with an empty ``Answer``
# and must not be read as "no answer" — same bug class as finding 1's
# transport-error case.
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_resolve_zone_ns_servfail_raises_and_queries_once(
    doh_client: AsyncMock,
) -> None:
    calls: list[str] = []

    async def fake_get(url: str, params: dict[str, str]) -> AsyncMock:
        calls.append(params["name"])
        return _fake_response({"Status": 2, "Answer": []})

    doh_client.get = AsyncMock(side_effect=fake_get)
    with pytest.raises(DnsLookupError):
        await resolve_zone_ns("acme.co.uk")
    assert calls == ["acme.co.uk"]


@pytest.mark.asyncio
async def test_resolve_zone_ns_nxdomain_continues_climbing(
    doh_client: AsyncMock,
) -> None:
    """NXDOMAIN (Status 3) is a genuine "no answer" — the walk must still
    climb to the next label, unlike SERVFAIL."""
    responses = {
        "mail.example.com": {"Status": 3, "Answer": []},
        "example.com": {
            "Status": 0,
            "Answer": [{"type": 2, "data": "ns1.example.com."}],
        },
    }
    calls: list[str] = []

    async def fake_get(url: str, params: dict[str, str]) -> AsyncMock:
        calls.append(params["name"])
        return _fake_response(responses[params["name"]])

    doh_client.get = AsyncMock(side_effect=fake_get)
    zone, nameservers = await resolve_zone_ns("mail.example.com")
    assert zone == "example.com"
    assert nameservers == ["ns1.example.com"]
    assert calls == ["mail.example.com", "example.com"]


@pytest.mark.asyncio
async def test_observe_record_servfail_raises_dns_lookup_error(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(return_value=_fake_response({"Status": 2, "Answer": []}))
    with pytest.raises(DnsLookupError):
        await observe_record(
            {
                "type": "CNAME",
                "name": "s1._domainkey.acme.com",
                "value": "target.example.com",
            }
        )


@pytest.mark.asyncio
async def test_observe_record_nxdomain_returns_false(doh_client: AsyncMock) -> None:
    doh_client.get = AsyncMock(return_value=_fake_response({"Status": 3, "Answer": []}))
    observed = await observe_record(
        {
            "type": "CNAME",
            "name": "s1._domainkey.acme.com",
            "value": "target.example.com",
        }
    )
    assert observed is False


@pytest.mark.asyncio
async def test_resolve_mx_servfail_returns_empty_list_unchanged(
    doh_client: AsyncMock,
) -> None:
    """resolve_mx's raise_on_error=True path keeps today's outward
    behaviour: it never inspected Status before this fix (only
    httpx/JSON errors raise), so a SERVFAIL response silently yields an
    empty list, same as before — check_domain and verify already depend on
    that (see _resolve's docstring)."""
    doh_client.get = AsyncMock(return_value=_fake_response({"Status": 2, "Answer": []}))
    assert await resolve_mx("acme.com") == []


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
        ("squarespace", "ns1.squarespacedns.com"),
        ("vercel", "ns1.vercel-dns.com"),
        ("digitalocean", "ns1.digitalocean.com"),
        ("ionos", "ns1067.ui-dns.com"),
        ("ionos", "ns1045.ui-dns.de"),
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


def test_detect_dns_provider_suffix_does_not_match_as_bare_substring() -> None:
    """ "hover.com" must match as a suffix, not anywhere in the nameserver —
    ns1.hover.company.com is a different (unknown) domain, not Hover."""
    assert detect_dns_provider(["ns.hover.company.com"]) is None


def test_detect_dns_provider_suffix_does_not_match_a_different_tail() -> None:
    """hover.com followed by more labels is not a match for the Hover suffix."""
    assert detect_dns_provider(["hover.com.evil.net"]) is None


def test_detect_dns_provider_suffix_matches_case_and_trailing_dot_insensitively() -> (
    None
):
    provider = detect_dns_provider(["NS1.HOVER.COM."])
    assert provider is not None
    assert provider.id == "hover"


def test_detect_dns_provider_ionos_fragment_anchored_at_label_boundary() -> None:
    """The ui-dns fragment must anchor on a label start (".ui-dns.") so it
    cannot match mid-label — only real IONOS-shaped nameservers qualify."""
    assert detect_dns_provider(["ns1.notui-dns.com"]) is None


def test_detect_dns_provider_vercel_dns_url_points_at_domains_page() -> None:
    """Finding 6: not the generic dashboard landing page."""
    provider = detect_dns_provider(["ns1.vercel-dns.com"])
    assert provider is not None
    assert provider.dns_url == "https://vercel.com/dashboard/domains"


def test_detect_dns_provider_porkbun_dns_url_is_the_login_page() -> None:
    """Finding 6: the deep-link URL could not be verified against
    Porkbun's own docs, so this falls back to the login page."""
    provider = detect_dns_provider(["curitiba.ns.porkbun.com"])
    assert provider is not None
    assert provider.dns_url == "https://porkbun.com/account/login"


def test_detect_dns_provider_google_cloud_dns_returns_none() -> None:
    """Legacy Google Domains and Google Cloud DNS both hand out
    ns-cloud-*.googledomains.com nameservers, so this suffix cannot tell
    them apart — Google Cloud DNS customers manage DNS in GCP, not
    Squarespace, so the table has no Google entry and this must be None."""
    provider = detect_dns_provider(
        ["ns-cloud-a1.googledomains.com", "ns-cloud-a2.googledomains.com"]
    )
    assert provider is None


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
async def test_observe_record_raises_dns_lookup_error_on_doh_failure(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DnsLookupError):
        await observe_record(
            {
                "type": "CNAME",
                "name": "s1._domainkey.acme.com",
                "value": "target.example.com",
            }
        )


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
async def test_dmarc_present_raises_dns_lookup_error_on_doh_failure(
    doh_client: AsyncMock,
) -> None:
    doh_client.get = AsyncMock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(DnsLookupError):
        await dmarc_present("acme.com")
