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
from hailhq.api.agent_gate import RATE_LIMITED_RESPONSES
from hailhq.api.main import app
from hailhq.api.ratelimit import GENERAL_RATE_LIMITED_RESPONSES
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
    # Exact IETF-draft header name only — GeneralRateLimitMiddleware sets
    # these itself (see ratelimit.py), it does not go through slowapi's own
    # header injection, which defaults to X-RateLimit-*. A regression to
    # that default must fail this assertion, not slide past an "or".
    assert "ratelimit-limit" in resp.headers
    assert "ratelimit-remaining" in resp.headers
    assert "ratelimit-reset" in resp.headers


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


@pytest.mark.parametrize("mount_prefix", ["", "/v1"])
async def test_sms_inbound_is_not_rate_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, mount_prefix: str
) -> None:
    # POST /sms/inbound has no Authorization header by design (Twilio
    # signature auth). Without the exemption every anonymous caller falls
    # into the shared remote-IP bucket in production. Bad signature -> 403,
    # never 429, regardless of the ceiling or how many times it's called.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    for _ in range(3):
        resp = await client.post(
            f"{mount_prefix}/sms/inbound",
            data={"From": "+14155551234", "To": "+14155559999", "Body": "hi"},
            headers={"X-Twilio-Signature": "sha1=bogus"},
        )
        assert resp.status_code == 403
        assert "ratelimit-limit" not in resp.headers


@pytest.mark.parametrize("mount_prefix", ["", "/v1"])
async def test_sms_status_is_not_rate_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, mount_prefix: str
) -> None:
    # POST /sms/status — same rationale as /sms/inbound above.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    for _ in range(3):
        resp = await client.post(
            f"{mount_prefix}/sms/status",
            data={"MessageSid": "SM1", "MessageStatus": "delivered"},
            headers={"X-Twilio-Signature": "sha1=bogus"},
        )
        assert resp.status_code == 403
        assert "ratelimit-limit" not in resp.headers


@pytest.mark.parametrize("mount_prefix", ["", "/v1"])
async def test_unsubscribe_is_not_rate_limited(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch, mount_prefix: str
) -> None:
    # GET /unsubscribe has no Authorization header by design (HMAC token
    # query param per RFC 8058, legally required to keep working). An
    # invalid/garbage token must never draw a 429 no matter the ceiling.
    monkeypatch.setattr(settings, "api_rate_limit_per_minute", 1)
    for _ in range(3):
        resp = await client.get(
            f"{mount_prefix}/unsubscribe", params={"token": "bogus"}
        )
        assert resp.status_code != 429
        assert "ratelimit-limit" not in resp.headers


@pytest.mark.parametrize("path", ["/v1/calls", "/v1/sms", "/v1/emails"])
def test_create_routes_merge_agent_gate_and_general_429_docs(path: str) -> None:
    # calls.py/sms.py/emails.py's create routes already documented
    # agent_gate.py's RATE_LIMITED_RESPONSES (the agent-abuse velocity-cap
    # 429) before this task. This task must MERGE its own
    # GENERAL_RATE_LIMITED_RESPONSES into that 429, not clobber it — both
    # causes are real and independently reachable on these 3 routes. Assert
    # against the live app.openapi() output (not the ratelimit.py helper in
    # isolation), so a Task 3 edit to these same decorators that
    # accidentally drops one side's responses= would fail this test.
    schema = app.openapi()
    responses = schema["paths"][path]["post"]["responses"]
    assert "429" in responses
    entry = responses["429"]

    # Both real 429 causes are represented in the merged description —
    # checked against the actual source strings, not a hardcoded guess.
    agent_gate_description = RATE_LIMITED_RESPONSES[429]["description"]
    general_description = GENERAL_RATE_LIMITED_RESPONSES[429]["description"]
    assert agent_gate_description in entry["description"]
    assert general_description in entry["description"]
    assert "velocity cap" in entry["description"]
    assert "general request-rate" in entry["description"]

    # All 4 headers survive the merge (Retry-After is declared by both
    # source dicts and collapses to one entry; the general limiter's other
    # 3 headers are additive).
    header_names = set(entry["headers"].keys())
    assert header_names == {
        "Retry-After",
        "RateLimit-Limit",
        "RateLimit-Remaining",
        "RateLimit-Reset",
    }
