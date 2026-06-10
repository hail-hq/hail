import socket

import pytest

from hailhq.core.http_post import (
    PrivateNetworkBlockedError,
    httpx_post,
    is_private_url,
    validate_webhook_target,
)


def test_is_private_url_localhost():
    assert is_private_url("http://localhost:8080/x")
    assert is_private_url("https://127.0.0.1/x")


def test_is_private_url_rfc1918():
    assert is_private_url("http://10.0.0.1/x")
    assert is_private_url("http://192.168.1.1/x")
    assert is_private_url("http://172.16.0.1/x")


def test_is_private_url_link_local():
    assert is_private_url("http://169.254.1.1/x")


def test_is_private_url_public_negative():
    assert not is_private_url("https://api.example.com/x")


def test_is_private_url_ipv6_loopback():
    assert is_private_url("http://[::1]:8080/x")


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
async def test_httpx_post_returns_status_and_body(respx_mock):
    respx_mock.post("https://example.com/x").respond(200, text="ok")
    status, body = await httpx_post(
        "https://example.com/x", b"{}", {}, allow_private_networks=False
    )
    assert status == 200
    assert body == "ok"


# ---------------------------------------------------------------------------
# validate_webhook_target tests
# ---------------------------------------------------------------------------


def test_validate_webhook_target_https_ok():
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
    assert is_private_url("https://[::1]/hook")


def test_ipv6_only_hostname_resolving_private_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        # AAAA-only host resolving to IPv6 loopback
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 0, 0, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert is_private_url("https://internal.example.com/hook")


def test_mixed_records_any_private_is_private(monkeypatch):
    def fake_getaddrinfo(host, port, **kw):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    assert is_private_url("https://rebind.example.com/hook")
