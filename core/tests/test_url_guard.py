"""assert_public_https_url: https-only + block private/loopback/metadata hosts."""

from __future__ import annotations

import re
import socket

import hailhq.core.url_guard as guard
import httpx2
import pytest
from hailhq.core.url_guard import NOT_PUBLIC, UnsafeUrlError, assert_public_https_url


def test_public_https_passes() -> None:
    assert (
        assert_public_https_url("https://api.openai.com/v1")
        == "https://api.openai.com/v1"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://api.openai.com/v1",  # not https
        "ftp://api.openai.com",  # not https
        "https://localhost/v1",  # loopback name
        "https://127.0.0.1/v1",  # loopback ip
        "https://[::1]/v1",  # loopback ipv6
        "https://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "https://10.0.0.5/v1",  # RFC1918
        "https://192.168.1.1/v1",  # RFC1918
        "https://172.16.0.1/v1",  # RFC1918
        "https://0.0.0.0/v1",  # unspecified
        "https://[::ffff:169.254.169.254]/v1",  # ipv4-mapped metadata
        "https://100.100.100.200/v1",  # CGNAT (cloud metadata, Tailscale)
        "https://[fec0::1]/v1",  # IPv6 site-local
        "https://224.0.0.1/v1",  # multicast
        "https:///v1",  # no host
        "not-a-url",
    ],
)
def test_unsafe_urls_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url(url)


def test_private_host_by_name_is_rejected(monkeypatch) -> None:
    # A public-looking hostname that resolves to a private IP must still fail.
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(None, None, None, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url("https://sneaky.example.com/v1")


def test_any_private_resolved_address_rejects(monkeypatch) -> None:
    # A host resolving to both a public and a private address must be rejected
    # (pins the "any private => reject" loop against future refactors).
    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (None, None, None, "", ("93.184.216.34", 0)),  # public
            (None, None, None, "", ("10.1.2.3", 0)),  # private
        ]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url("https://dual.example.com/v1")


# ---------------------------------------------------------------------------
# IDNA: the guard must resolve the exact host httpx connects to (IDNA 2008),
# not the stdlib IDNA 2003 form (which maps ß -> ss, ς -> σ, drops ZWJ).
# ---------------------------------------------------------------------------

# host -> resolved IP. The IDNA 2003 form is public, the IDNA 2008 form (the
# one httpx really connects to) is private: the attack the guard must stop.
_SPLIT_DNS = {
    "fass.example": "93.184.216.34",
    "xn--fa-hia.example": "10.1.2.3",  # faß.example under IDNA 2008
    "xn--nxasmq6b.example": "93.184.216.34",  # βόλοσ.example (IDNA 2003)
    "xn--nxasmm1c.example": "10.1.2.4",  # βόλος.example under IDNA 2008
    "xn--bcher-kva.example": "93.184.216.34",  # bücher.example (same in both)
}


@pytest.fixture
def split_dns(monkeypatch) -> list[str]:
    asked: list[str] = []

    def fake_getaddrinfo(host, *args, **kwargs):
        asked.append(host)
        if host not in _SPLIT_DNS:
            raise socket.gaierror(socket.EAI_NONAME, "not known")
        return [(None, None, None, "", (_SPLIT_DNS[host], 0))]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    return asked


@pytest.mark.parametrize(
    ("url", "httpx_host"),
    [
        ("https://faß.example/v1", "xn--fa-hia.example"),  # sharp s
        ("https://FAß.Example/v1", "xn--fa-hia.example"),  # mixed case
        ("https://βόλος.example/v1", "xn--nxasmm1c.example"),  # final sigma
    ],
)
def test_idna_2008_host_is_the_one_checked(
    split_dns: list[str], url: str, httpx_host: str
) -> None:
    with pytest.raises(UnsafeUrlError, match=re.escape(NOT_PUBLIC)):
        assert_public_https_url(url)
    assert split_dns == [httpx_host]


def test_zero_width_joiner_host_rejected_without_lookup(split_dns: list[str]) -> None:
    # IDNA 2003 silently drops U+200D (fa\u200dss -> fass); IDNA 2008 refuses
    # it outside a virama context, so httpx would never send this URL.
    with pytest.raises(UnsafeUrlError, match="not valid"):
        assert_public_https_url("https://fa\u200dss.example/v1")
    assert split_dns == []


def test_normal_idn_host_still_works(split_dns: list[str]) -> None:
    assert (
        assert_public_https_url("https://bücher.example/v1/")
        == "https://bücher.example/v1"
    )
    assert split_dns == ["xn--bcher-kva.example"]


@pytest.mark.parametrize(
    "url",
    [
        "https://fa\tss.example/v1",  # tab (urlsplit strips it silently)
        "https://fa\nss.example/v1",  # newline
        "https://fa\rss.example/v1",  # carriage return
        "https://fass.example\x00.evil/v1",  # NUL
        "https://fa\x7fss.example/v1",  # DEL
        "https://fa\x85ss.example/v1",  # C1 next-line
        "https://fass.example/v1\n",  # control char outside the host too
    ],
)
def test_control_characters_rejected_without_lookup(
    split_dns: list[str], url: str
) -> None:
    with pytest.raises(UnsafeUrlError, match="control character"):
        assert_public_https_url(url)
    assert split_dns == []


@pytest.mark.parametrize(
    "url",
    [
        "https://api.openai.com:99999/v1",  # port out of range
        "https://api.openai.com:abc/v1",  # port not a number
        "https://a..b.example/v1",  # empty label: stdlib idna raises UnicodeError
    ],
)
def test_malformed_host_or_port_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://faß.example/v1",
        "https://FAß.Example/v1",
        "https://βόλος.example/v1",
        "https://bücher.example/v1",
        "https://api.openai.com/v1",
        "https://[::ffff:10.0.0.1]/v1",
    ],
)
def test_httpx2_connects_to_the_same_host(url: str) -> None:
    # The openai SDK sends BYO LLM requests with httpx2, a separate package
    # from the httpx the guard parses with. Fail if their hosts ever differ.
    # httpx2 is always installed in CI (`uv sync --all-packages`).
    assert guard.httpx_url(url).raw_host == httpx2.URL(url).raw_host


def test_error_text_never_shows_the_resolved_address(split_dns: list[str]) -> None:
    # Customers must not learn what our resolver answers for internal names.
    with pytest.raises(UnsafeUrlError) as excinfo:
        assert_public_https_url("https://faß.example/v1")
    assert "10.1.2.3" not in str(excinfo.value)
    assert str(excinfo.value) == NOT_PUBLIC


def test_port_zero_rejected() -> None:
    with pytest.raises(UnsafeUrlError, match="invalid port"):
        assert_public_https_url("https://api.openai.com:0/v1")


def test_unreadable_resolved_address_fails_closed(monkeypatch) -> None:
    def fake_getaddrinfo(host, *args, **kwargs):
        return [(None, None, None, "", ("not-an-ip", 0))]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError, match=re.escape(NOT_PUBLIC)):
        assert_public_https_url("https://odd.example.com/v1")


@pytest.mark.parametrize(
    "answer",
    [
        [],  # resolver gave no addresses
        [(None, None, None, "", ("127.0.0.1", 0))],  # localhost / loopback
    ],
)
def test_every_refusal_uses_the_same_text(monkeypatch, answer) -> None:
    def fake_getaddrinfo(host, *args, **kwargs):
        return answer

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError) as excinfo:
        assert_public_https_url("https://localhost/v1")
    assert str(excinfo.value) == NOT_PUBLIC
