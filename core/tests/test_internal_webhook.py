"""Tests for fetch_organization_name — every failure mode folds to None."""

from __future__ import annotations

import asyncio

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
    assert kwargs["timeout"].total == 1.0


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
