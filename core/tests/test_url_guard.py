"""assert_public_https_url: https-only + block private/loopback/metadata hosts."""

from __future__ import annotations

import pytest
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url


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
        "https:///v1",  # no host
        "https://" + "a" * 64 + ".com/v1",  # label too long to look up
        "not-a-url",
    ],
)
def test_unsafe_urls_rejected(url: str) -> None:
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url(url)


def test_private_host_by_name_is_rejected(monkeypatch) -> None:
    # A public-looking hostname that resolves to a private IP must still fail.
    import hailhq.core.url_guard as guard

    def fake_getaddrinfo(host, *args, **kwargs):
        return [(None, None, None, "", ("10.1.2.3", 0))]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url("https://sneaky.example.com/v1")


def test_any_private_resolved_address_rejects(monkeypatch) -> None:
    # A host resolving to both a public and a private address must be rejected
    # (pins the "any private => reject" loop against future refactors).
    import hailhq.core.url_guard as guard

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (None, None, None, "", ("93.184.216.34", 0)),  # public
            (None, None, None, "", ("10.1.2.3", 0)),  # private
        ]

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url("https://dual.example.com/v1")


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com\x00.internal/v1",  # getaddrinfo stops at NUL
        "https://a\x85b.com/v1",  # C1 control
        "https://a\u00a0b.com/v1",  # no-break space
    ],
)
def test_space_or_control_character_is_refused(url: str) -> None:
    with pytest.raises(UnsafeUrlError, match="space or control character"):
        assert_public_https_url(url)


def test_a_resolver_system_error_is_an_unsafe_url(monkeypatch) -> None:
    import hailhq.core.url_guard as guard

    def fake_getaddrinfo(host, *args, **kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(guard.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(UnsafeUrlError):
        assert_public_https_url("https://api.example.com/v1")
