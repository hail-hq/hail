import re
import socket

import httpx
import pytest
from hailhq.core.http_post import (
    PrivateNetworkBlockedError,
    httpx_post,
    validate_webhook_target,
)
from hailhq.core.url_guard import NOT_PUBLIC, UnsafeUrlError


def _blocked(url: str) -> bool:
    """True if the webhook guard refuses ``url`` (no private-network opt-in)."""
    try:
        validate_webhook_target(url, allow_private_networks=False)
    except ValueError:
        return True
    return False


@pytest.fixture
def public_dns(monkeypatch):
    """Every name resolves to one public address (no real DNS in tests)."""

    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def test_blocks_localhost():
    assert _blocked("http://localhost:8080/x")
    assert _blocked("https://127.0.0.1/x")


def test_blocks_rfc1918():
    assert _blocked("http://10.0.0.1/x")
    assert _blocked("http://192.168.1.1/x")
    assert _blocked("http://172.16.0.1/x")


def test_blocks_link_local():
    assert _blocked("http://169.254.1.1/x")


def test_blocks_public_negative(public_dns):
    assert not _blocked("https://api.example.com/x")


def test_blocks_ipv6_loopback():
    assert _blocked("http://[::1]:8080/x")


@pytest.mark.asyncio
async def test_httpx_post_blocks_private_by_default():
    with pytest.raises(PrivateNetworkBlockedError):
        await httpx_post(
            "http://127.0.0.1:8080/x",
            b"{}",
            {},
            allow_private_networks=False,
        )


@pytest.mark.asyncio
async def test_httpx_post_allows_private_when_flag_true(respx_mock):
    respx_mock.post("http://127.0.0.1:8080/x").respond(204, text="")
    status, body = await httpx_post(
        "http://127.0.0.1:8080/x",
        b"{}",
        {},
        allow_private_networks=True,
    )
    assert status == 204
    assert body == ""


@pytest.mark.asyncio
async def test_httpx_post_returns_status_and_body(respx_mock, public_dns):
    route = respx_mock.post("https://93.184.216.34/x").respond(200, text="ok")
    status, body = await httpx_post(
        "https://example.com/x", b"{}", {}, allow_private_networks=False
    )
    assert status == 200
    assert body == "ok"
    sent = route.calls.last.request
    # Sent to the checked address, under the original name.
    assert sent.headers["host"] == "example.com"
    assert sent.extensions["sni_hostname"] == "example.com"


@pytest.mark.asyncio
async def test_httpx_post_connects_to_the_checked_address(respx_mock, monkeypatch):
    """A second DNS answer (a rebind to a private address) is never used."""
    answers = iter(["93.184.216.34", "169.254.169.254"])

    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (next(answers), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    route = respx_mock.post("https://93.184.216.34:8443/x").respond(204)
    status, _ = await httpx_post(
        "https://rebind.example.com:8443/x", b"{}", {}, allow_private_networks=False
    )
    assert status == 204
    assert route.calls.last.request.headers["host"] == "rebind.example.com:8443"


@pytest.mark.asyncio
async def test_httpx_post_stops_reading_at_max_body_bytes(respx_mock, public_dns):
    respx_mock.post("https://93.184.216.34/x").respond(200, text="a" * 10_000)
    status, body = await httpx_post(
        "https://example.com/x",
        b"{}",
        {},
        allow_private_networks=False,
        max_body_bytes=100,
    )
    assert status == 200
    assert body == "a" * 100


# ---------------------------------------------------------------------------
# validate_webhook_target tests
# ---------------------------------------------------------------------------


def test_validate_webhook_target_https_ok(public_dns):
    """A public https URL passes validation."""
    validate_webhook_target(
        "https://hooks.example.com/ingest", allow_private_networks=False
    )


def test_validate_webhook_target_http_rejected():
    """Plain http is rejected unless allow_private_networks is True."""
    with pytest.raises(ValueError, match="https"):
        validate_webhook_target(
            "http://hooks.example.com/x", allow_private_networks=False
        )


def test_validate_webhook_target_http_allowed_when_private_flag():
    """http is allowed when allow_private_networks=True (self-host escape hatch)."""
    validate_webhook_target("http://192.168.1.5/endpoint", allow_private_networks=True)


def test_validate_webhook_target_http_public_host_allowed_when_private_flag():
    """allow_private_networks=True accepts plain HTTP to public hosts too (blanket relaxation)."""
    validate_webhook_target("http://public.example.com/x", allow_private_networks=True)


def test_validate_webhook_target_private_ip_rejected():
    """A private IP (link-local here) is rejected when allow_private_networks=False."""
    with pytest.raises(ValueError, match="private"):
        validate_webhook_target(
            "https://169.254.169.254/meta", allow_private_networks=False
        )


def test_validate_webhook_target_private_allowed_when_flag():
    """Private targets are accepted when allow_private_networks=True."""
    validate_webhook_target("https://10.0.0.1/webhook", allow_private_networks=True)


def test_validate_webhook_target_missing_host_rejected():
    """A URL with no hostname is rejected (even https scheme)."""
    with pytest.raises(ValueError, match="host"):
        validate_webhook_target("https:///path/only", allow_private_networks=False)


# ---------------------------------------------------------------------------
# IPv6 / multi-record resolution tests
# ---------------------------------------------------------------------------


def test_ipv6_loopback_literal_is_private():
    assert _blocked("https://[::1]/hook")


def test_ipv6_only_hostname_resolving_private_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        # AAAA-only host resolving to IPv6 loopback
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _blocked("https://internal.example.com/hook")


def test_mixed_records_any_private_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert _blocked("https://rebind.example.com/hook")


# ---------------------------------------------------------------------------
# IDNA + control characters: check the host httpx really connects to
# ---------------------------------------------------------------------------


def _split_dns(monkeypatch) -> list[str]:
    # IDNA 2003 form public, IDNA 2008 form (what httpx uses) private.
    answers = {"fass.example": "93.184.216.34", "xn--fa-hia.example": "10.1.2.3"}
    asked: list[str] = []

    def fake_getaddrinfo(host, port, **kw):
        asked.append(host)
        if host not in answers:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (answers[host], 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return asked


def test_blocks_checks_idna_2008_host(monkeypatch):
    asked = _split_dns(monkeypatch)
    assert _blocked("https://faß.example/hook")
    assert asked == ["xn--fa-hia.example"]


def test_validate_webhook_target_checks_idna_2008_host(monkeypatch):
    asked = _split_dns(monkeypatch)
    with pytest.raises(ValueError, match="private"):
        validate_webhook_target(
            "https://faß.example/hook", allow_private_networks=False
        )
    assert asked == ["xn--fa-hia.example"]


@pytest.mark.parametrize(
    "url",
    [
        "https://fa\tss.example/hook",
        "https://fa\nss.example/hook",
        "https://a\x00b.example/",
    ],
)
def test_control_characters_refused(url):
    assert _blocked(url)
    with pytest.raises(ValueError, match="control character"):
        validate_webhook_target(url, allow_private_networks=False)
    with pytest.raises(ValueError, match="control character"):
        validate_webhook_target(url, allow_private_networks=True)


def test_cgnat_and_multicast_are_private():
    # Same classifier as the BYO base_url guard: 100.100.100.200 is a cloud
    # metadata address inside CGNAT space.
    assert _blocked("https://100.100.100.200/latest/meta-data")
    assert _blocked("https://224.0.0.1/hook")
    with pytest.raises(ValueError, match="private"):
        validate_webhook_target(
            "https://100.100.100.200/x", allow_private_networks=False
        )


@pytest.mark.asyncio
async def test_httpx_post_without_host_says_so():
    # A clear error, not PrivateNetworkBlockedError carrying the URL.
    with pytest.raises(UnsafeUrlError, match="URL has no host"):
        await httpx_post("https:///x.com", b"{}", {}, allow_private_networks=False)


def test_ipv6_site_local_is_private():
    assert _blocked("https://[fec0::1]/hook")


def test_unresolvable_host_is_blocked(monkeypatch):
    # Fail closed: a name the guard cannot resolve may resolve to a private
    # address a moment later, when httpx looks it up again.
    def fake_getaddrinfo(host, port, **kw):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(ValueError, match=re.escape(NOT_PUBLIC)):
        validate_webhook_target(
            "https://flaky.example.com/hook", allow_private_networks=False
        )


@pytest.mark.asyncio
async def test_httpx_post_blocks_unresolvable_host(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        raise socket.gaierror(socket.EAI_AGAIN, "temporary failure")

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(PrivateNetworkBlockedError, match=re.escape(NOT_PUBLIC)):
        await httpx_post(
            "https://flaky.example.com/hook",
            b"{}",
            {},
            allow_private_networks=False,
        )


@pytest.mark.asyncio
async def test_errors_never_show_the_resolved_address(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.4.2.7", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    url = "https://db.internal.example.com/x"
    with pytest.raises(ValueError) as write_err:
        validate_webhook_target(url, allow_private_networks=False)
    with pytest.raises(PrivateNetworkBlockedError) as send_err:
        await httpx_post(url, b"{}", {}, allow_private_networks=False)
    for err in (write_err.value, send_err.value):
        assert "10.4.2.7" not in str(err)
        assert NOT_PUBLIC in str(err)


@pytest.mark.asyncio
async def test_httpx_post_tries_ipv4_first_then_the_next_address(
    respx_mock, monkeypatch
):
    def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1", 0, 0, 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.35", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    first = respx_mock.post("https://93.184.216.34/x").mock(
        side_effect=httpx.ConnectError("refused")
    )
    second = respx_mock.post("https://93.184.216.35/x").respond(200, text="ok")
    status, body = await httpx_post(
        "https://example.com/x", b"{}", {}, allow_private_networks=False
    )
    assert (status, body) == (200, "ok")
    assert first.called and second.called
