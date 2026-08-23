"""Shared test fixtures for the mcp package's test suite."""

from __future__ import annotations

import pytest
from hailhq.mcp.discovery_auth import _reset_anon_init_rate_state_for_tests


@pytest.fixture(autouse=True)
def _reset_discovery_auth_rate_state():
    """DiscoveryAuthMiddleware's anonymous-initialize rate cap is a
    module-level, process-wide counter keyed by remote IP. httpx's
    ASGITransport defaults every test to the same synthetic client address
    (127.0.0.1:123 — see httpx.ASGITransport.__init__), so without this
    reset, one test's initialize calls would count against every other
    test's budget and could spuriously trip the cap. Resets before AND
    after each test so ordering never matters.
    """
    _reset_anon_init_rate_state_for_tests()
    yield
    _reset_anon_init_rate_state_for_tests()
