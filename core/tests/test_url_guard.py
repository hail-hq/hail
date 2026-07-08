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
        "https:///v1",  # no host
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
