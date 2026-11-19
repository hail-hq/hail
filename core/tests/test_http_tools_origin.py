"""When saved HTTP tool headers may stay: only on the same origin."""

from __future__ import annotations

from hailhq.core import http_tools
from yarl import URL


def test_an_idna_host_and_its_ascii_form_are_one_origin() -> None:
    assert http_tools._same_origin(
        "https://fa\u00df.example/a", "https://xn--fa-hia.example:443/b"
    )


def test_a_new_port_or_host_is_a_new_origin() -> None:
    assert not http_tools._same_origin(
        "https://api.example.com/", "https://api.example.com:8443/"
    )
    assert not http_tools._same_origin(
        "https://api.example.com/", "https://api.example.net/"
    )


def test_a_url_that_cannot_be_parsed_is_never_the_same_origin() -> None:
    # A no-break space: httpx refuses the host.
    url = "https://a\u00a0b.com/"
    assert not http_tools._same_origin(url, url)


def test_hosts_httpx_reads_alike_but_yarl_does_not_are_two_origins() -> None:
    # httpx reads both hosts as "a%20b.com"; yarl, which aiohttp connects
    # with, reads "a b.com" and "a%20b.com".
    spaced, encoded = "https://a b.com/", "https://a%20b.com/"
    assert http_tools._origin(spaced)[:3] == http_tools._origin(encoded)[:3]
    assert not http_tools._same_origin(spaced, encoded)


def test_a_new_url_httpx_refuses_is_a_new_origin() -> None:
    # yarl reads a fullwidth "a" (U+FF41) as "a"; httpx refuses the host.
    assert not http_tools._same_origin(
        "https://api.example.com/", "https://\uff41pi.example.com/"
    )


def test_yarl_must_agree_too(monkeypatch) -> None:
    """aiohttp connects with yarl: when yarl reads a different host than
    httpx, the headers are not kept."""

    def fake_url(url: str) -> URL:
        return URL("https://evil.example/") if "/b" in url else URL(url)

    monkeypatch.setattr(http_tools, "URL", fake_url)
    assert not http_tools._same_origin(
        "https://api.example.com/a", "https://api.example.com/b"
    )
