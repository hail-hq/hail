"""Tests for the general per-caller HTTP request-rate limiter (ratelimit.py).

Distinct from tests/test_agent_gate_api.py (the agent-abuse velocity cap) —
this limiter applies to every customer-facing route, for every caller, keyed
on the raw bearer (or remote address when there is none), not on org/channel
send velocity.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from hailhq.core.config import settings
from hailhq.core.models import ApiKey


async def test_response_carries_ratelimit_headers(
    client: httpx.AsyncClient, org_and_key: tuple[uuid.UUID, ApiKey, str]
) -> None:
    _, _, plain_key = org_and_key
    resp = await client.get(
        "/v1/whoami", headers={"Authorization": f"Bearer {plain_key}"}
    )
    assert resp.status_code == 200
    assert "ratelimit-limit" in resp.headers or "x-ratelimit-limit" in resp.headers


async def test_exceeding_the_limit_returns_429_with_retry_after(
    client: httpx.AsyncClient,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, plain_key = org_and_key
    # Drop the ceiling to something trivially exceedable within one test,
    # rather than firing 300+ real requests. The middleware reads
    # settings.api_rate_limit_per_minute fresh on every request (it is not
    # captured once at app-startup/import time), so this monkeypatch takes
    # effect immediately.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    headers = {"Authorization": f"Bearer {plain_key}"}
    for _ in range(2):
        resp = await client.get("/v1/whoami", headers=headers)
        assert resp.status_code == 200
    resp = await client.get("/v1/whoami", headers=headers)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


async def test_both_mounts_of_the_same_route_share_one_bucket(
    client: httpx.AsyncClient,
    org_and_key: tuple[uuid.UUID, ApiKey, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Task 1 dual-mounts every customer router at /v1/<resource> and the
    # legacy unprefixed path. The limiter is keyed on the raw bearer, not
    # the matched route, so hitting both mounts of the same caller must
    # draw from the same bucket.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 2)
    _, _, plain_key = org_and_key
    headers = {"Authorization": f"Bearer {plain_key}"}
    resp = await client.get("/v1/whoami", headers=headers)
    assert resp.status_code == 200
    resp = await client.get("/whoami", headers=headers)
    assert resp.status_code == 200
    resp = await client.get("/v1/whoami", headers=headers)
    assert resp.status_code == 429
    assert "retry-after" in resp.headers


async def test_internal_routes_are_not_rate_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Internal routes are called by internal services on a trusted,
    # HMAC-signed path, not customer API keys — no Authorization header at
    # all. Confirm the general limiter's key_func doesn't apply to (or
    # crash on) an unauthenticated internal request, by dropping the
    # ceiling low and firing more than that many requests without ever
    # seeing a 429.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    for _ in range(3):
        resp = await client.post(
            "/internal/numbers/release",
            content=b"{}",
            headers={"Content-Type": "application/json"},
        )
        # No valid HMAC signature -> auth failure, never a 429 from the
        # general limiter (which isn't applied to /internal/* at all).
        assert resp.status_code != 429


async def test_healthz_is_not_rate_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    for _ in range(3):
        resp = await client.get("/healthz")
        assert resp.status_code == 200
        assert "ratelimit-limit" not in resp.headers
