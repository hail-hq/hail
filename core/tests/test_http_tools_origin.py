"""When saved HTTP tool headers may stay: only on the same origin."""

from __future__ import annotations

from hailhq.core import http_tools
from hailhq.core.http_tools import _same_origin
from yarl import URL


def test_an_idna_host_and_its_ascii_form_are_one_origin() -> None:
    assert _same_origin("https://faß.example/a", "https://xn--fa-hia.example:443/b")


def test_a_new_port_or_host_is_a_new_origin() -> None:
    assert not _same_origin("https://api.example.com/", "https://api.example.com:8443/")
    assert not _same_origin("https://api.example.com/", "https://api.example.net/")


def test_a_url_that_cannot_be_parsed_is_never_the_same_origin() -> None:
    assert not _same_origin("https://a b.com/", "https://a b.com/")


def test_yarl_must_agree_too(monkeypatch) -> None:
    """aiohttp connects with yarl: when yarl reads a different host than
    httpx, the headers are not kept."""

    def fake_url(url: str) -> URL:
        return URL("https://evil.example/") if "/b" in url else URL(url)

    monkeypatch.setattr(http_tools, "URL", fake_url)
    assert not _same_origin("https://api.example.com/a", "https://api.example.com/b")
