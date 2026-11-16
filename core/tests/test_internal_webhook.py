"""Tests for fetch_organization_name: every failure mode folds to the last
name seen, else None."""

from __future__ import annotations

import asyncio
import time

import aiohttp
import pytest
from hailhq.core import internal_webhook
from hailhq.core.config import settings
from hailhq.core.internal_webhook import fetch_organization_name


class _FakeResponse:
    def __init__(self, status=200, payload=None, json_exc=None):
        self.status = status
        self._payload = payload
        self._json_exc = json_exc

    async def json(self):
        if self._json_exc is not None:
            raise self._json_exc
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response


@pytest.fixture(autouse=True)
def empty_cache(monkeypatch):
    monkeypatch.setattr(internal_webhook, "_org_names", {})


@pytest.fixture()
def configured(monkeypatch):
    monkeypatch.setattr(settings, "hail_base_url", "https://hail.so")
    monkeypatch.setattr(settings, "hail_internal_secret", "test-secret")


def _install(monkeypatch, session: _FakeSession) -> None:
    monkeypatch.setattr(internal_webhook, "_get_session", lambda: session)


async def test_returns_name_on_200(monkeypatch, configured):
    session = _FakeSession(response=_FakeResponse(200, {"name": "  Acme Corp  "}))
    _install(monkeypatch, session)

    assert await fetch_organization_name("org-123") == "Acme Corp"

    url, kwargs = session.calls[0]
    assert url == "https://hail.so/api/internal/organizations/lookup"
    assert kwargs["headers"]["X-Hail-Signature"].startswith("sha256=")
    assert kwargs["timeout"].total == 2.0


async def test_unset_config_returns_none_without_any_network_call(monkeypatch):
    monkeypatch.setattr(settings, "hail_base_url", "")
    monkeypatch.setattr(settings, "hail_internal_secret", "")

    def _boom():  # pragma: no cover — proves _get_session is never reached
        raise AssertionError("network layer must not be touched")

    monkeypatch.setattr(internal_webhook, "_get_session", _boom)
    assert await fetch_organization_name("org-123") is None


async def test_timeout_returns_none(monkeypatch, configured):
    _install(monkeypatch, _FakeSession(exc=asyncio.TimeoutError()))
    assert await fetch_organization_name("org-123") is None


async def test_non_200_returns_none(monkeypatch, configured):
    for status in (404, 500):
        _install(monkeypatch, _FakeSession(response=_FakeResponse(status)))
        assert await fetch_organization_name("org-123") is None


async def test_connection_error_returns_none(monkeypatch, configured):
    _install(monkeypatch, _FakeSession(exc=aiohttp.ClientConnectionError()))
    assert await fetch_organization_name("org-123") is None


async def test_malformed_body_returns_none(monkeypatch, configured):
    # json() raises (non-JSON body) …
    _install(
        monkeypatch,
        _FakeSession(response=_FakeResponse(200, json_exc=ValueError("not json"))),
    )
    assert await fetch_organization_name("org-123") is None
    # … or parses but has no usable name.
    for payload in ({"nope": 1}, {"name": ""}, {"name": "   "}, ["x"], None):
        _install(monkeypatch, _FakeSession(response=_FakeResponse(200, payload)))
        assert await fetch_organization_name("org-123") is None


async def test_aclose_waits_for_pending_notifier_tasks(monkeypatch):
    """aclose drains in-flight fire-and-forget tasks before closing the
    session — a shutdown mid-POST must not abort the send or let a
    late task lazily create a session nothing closes."""
    started = asyncio.Event()
    release = asyncio.Event()
    finished = asyncio.Event()

    async def slow_post():
        started.set()
        await release.wait()
        finished.set()

    task = asyncio.create_task(slow_post())
    internal_webhook._pending_tasks.add(task)
    task.add_done_callback(internal_webhook._pending_tasks.discard)
    await started.wait()

    # Let aclose's drain complete promptly once it starts waiting.
    asyncio.get_running_loop().call_later(0.01, release.set)
    await internal_webhook.aclose()

    assert finished.is_set(), "aclose returned before the pending task finished"
    assert not internal_webhook._pending_tasks


async def test_fresh_name_is_served_from_cache(monkeypatch, configured):
    session = _FakeSession(response=_FakeResponse(200, {"name": "Acme Corp"}))
    _install(monkeypatch, session)

    assert await fetch_organization_name("org-123") == "Acme Corp"
    assert await fetch_organization_name("org-123") == "Acme Corp"
    assert len(session.calls) == 1


async def test_stale_name_survives_a_failed_refresh(monkeypatch, configured):
    stale = time.monotonic() - 2 * internal_webhook._ORG_NAME_TTL_SECONDS
    monkeypatch.setattr(
        internal_webhook, "_org_names", {"org-123": ("Acme Corp", stale)}
    )
    session = _FakeSession(exc=asyncio.TimeoutError())
    _install(monkeypatch, session)

    assert await fetch_organization_name("org-123") == "Acme Corp"
    assert len(session.calls) == 1


@pytest.mark.parametrize(
    "payload,allowed",
    [
        ({}, False),
        ({"toll_free": {"supported": True}}, False),
        (
            {"toll_free": {"supported": True, "filing_cents": 1, "monthly_cents": 0}},
            False,
        ),
        (
            {
                "toll_free": {
                    "supported": True,
                    "filing_cents": False,
                    "monthly_cents": 0,
                }
            },
            False,
        ),
        (
            {"toll_free": {"supported": True, "filing_cents": 0, "monthly_cents": 0}},
            True,
        ),
    ],
)
async def test_tollfree_capability_requires_explicit_free_support(
    monkeypatch, configured, payload, allowed
):
    session = _FakeSession(response=_FakeResponse(200, payload))
    _install(monkeypatch, session)
    assert await internal_webhook.tollfree_verification_supported() is allowed
    assert (
        session.calls[0][0]
        == "https://hail.so/api/internal/sms-registrations/capabilities"
    )
    assert session.calls[0][1]["data"] == b"{}"


async def test_tollfree_capability_is_open_on_self_hosted_install(monkeypatch):
    # No website: every org on the install files toll-free, with no call out.
    session = _FakeSession(response=_FakeResponse(500, {}))
    _install(monkeypatch, session)
    monkeypatch.setattr(internal_webhook.settings, "hail_base_url", "")
    assert await internal_webhook.tollfree_verification_supported() is True
    assert session.calls == []


async def test_tollfree_capability_fails_closed_without_secret(monkeypatch):
    session = _FakeSession(response=_FakeResponse(200, {}))
    _install(monkeypatch, session)
    monkeypatch.setattr(internal_webhook.settings, "hail_base_url", "https://hail.so")
    monkeypatch.setattr(internal_webhook.settings, "hail_internal_secret", "")
    assert await internal_webhook.tollfree_verification_supported() is False
    assert session.calls == []


# --------------------------------------------------------------------------- #
# quote_call_price
# --------------------------------------------------------------------------- #

_LEG = {
    "direction": "outbound",
    "carrier": "twilio",
    "number_e164": "+2348100000000",
    "caller_e164": "+14155550100",
}


async def test_quote_posts_signed_legs_and_reads_prices(monkeypatch, configured):
    import json

    from hailhq.core import hmac_signing
    from hailhq.core.internal_webhook import quote_call_price

    session = _FakeSession(
        _FakeResponse(payload={"cents_per_minute": 42.5, "cents_per_call": 0})
    )
    _install(monkeypatch, session)
    quote = await quote_call_price("org-1", [_LEG], True)
    assert quote == (internal_webhook.Decimal("42.5"), internal_webhook.Decimal(0))
    url, kwargs = session.calls[0]
    assert url == "https://hail.so/api/internal/voice-rates/quote"
    body = kwargs["data"]
    assert json.loads(body) == {
        "organization_id": "org-1",
        "recording": True,
        "legs": [_LEG],
    }
    assert kwargs["headers"]["X-Hail-Signature"] == hmac_signing.sign(
        body, "test-secret"
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"cents_per_minute": 0},
        {"cents_per_minute": -1},
        {"cents_per_minute": "NaN"},
        {"cents_per_minute": 10, "cents_per_call": -1},
        {"cents_per_call": 1},
        ["not", "an", "object"],
    ],
)
async def test_quote_out_of_range_is_none(monkeypatch, configured, payload):
    from hailhq.core.internal_webhook import quote_call_price

    _install(monkeypatch, _FakeSession(_FakeResponse(payload=payload)))
    assert await quote_call_price("org-1", [_LEG], False) is None


async def test_quote_failures_are_none(monkeypatch, configured):
    from hailhq.core.internal_webhook import quote_call_price

    _install(monkeypatch, _FakeSession(_FakeResponse(status=503)))
    assert await quote_call_price("org-1", [_LEG], False) is None
    _install(monkeypatch, _FakeSession(exc=asyncio.TimeoutError()))
    assert await quote_call_price("org-1", [_LEG], False) is None


async def test_quote_unconfigured_is_none(monkeypatch):
    from hailhq.core.internal_webhook import quote_call_price, quotes_enabled

    monkeypatch.setattr(settings, "hail_base_url", "")
    assert quotes_enabled() is False
    assert await quote_call_price("org-1", [_LEG], False) is None
