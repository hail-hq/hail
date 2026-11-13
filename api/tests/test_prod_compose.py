"""The prod API trusts proxy headers only while its port stays local."""

from __future__ import annotations

from pathlib import Path

import yaml

_COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.prod.yml"


class _Loader(yaml.SafeLoader):
    """Reads compose's !reset and !override tags as plain values."""


_Loader.add_constructor("!reset", lambda loader, node: None)
_Loader.add_constructor(
    "!override",
    lambda loader, node: (
        loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
    ),
)


def _api() -> dict:
    return yaml.load(_COMPOSE.read_text(), Loader=_Loader)["services"]["api"]


def test_api_trusts_caddys_forwarded_header() -> None:
    assert _api()["environment"]["FORWARDED_ALLOW_IPS"] == "*"


def test_api_port_is_bound_to_localhost_only() -> None:
    """Trusting the header is safe only when Caddy is the one way in."""
    ports = _api()["ports"]
    assert ports, "the api must publish its port for the host check"
    assert all(str(p).startswith("127.0.0.1:") for p in ports)
