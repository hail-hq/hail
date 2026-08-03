"""Tests for the boot-time MCP auth-mode selector.

The MCP service runs in exactly one of two modes, decided at startup
from the env. The selector is the single source of truth so server.py
and tools.py never branch on env directly.
"""

from __future__ import annotations

import pytest
from hailhq.mcp.auth import AuthMode, PassThroughVerifier, select_auth_mode


def test_oauth_rs_mode_when_only_hail_auth_url_set(monkeypatch):
    monkeypatch.setattr(
        "hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth"
    )
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
    assert select_auth_mode() is AuthMode.OAUTH_RS


def test_static_key_mode_when_only_hail_api_key_set(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_xxx")
    assert select_auth_mode() is AuthMode.STATIC_KEY


def test_both_set_raises_with_clear_message(monkeypatch):
    monkeypatch.setattr(
        "hailhq.core.config.settings.hail_auth_url", "https://hail.so/api/auth"
    )
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "hl_live_xxx")
    with pytest.raises(RuntimeError, match="ambiguous MCP auth config"):
        select_auth_mode()


def test_neither_set_raises_with_clear_message(monkeypatch):
    monkeypatch.setattr("hailhq.core.config.settings.hail_auth_url", "")
    monkeypatch.setattr("hailhq.core.config.settings.hail_api_key", "")
    with pytest.raises(RuntimeError, match="MCP auth not configured"):
        select_auth_mode()


@pytest.mark.asyncio
async def test_pass_through_verifier_accepts_any_non_empty_token():
    v = PassThroughVerifier(resource_server_url="https://mcp.hail.so")
    tok = await v.verify_token("opaque-bearer-value")
    assert tok is not None
    assert tok.token == "opaque-bearer-value"
    assert tok.scopes == []
    # Resource is the audience we expect the API to accept this token for.
    assert tok.resource == "https://mcp.hail.so"


@pytest.mark.asyncio
async def test_pass_through_verifier_rejects_empty_token():
    v = PassThroughVerifier(resource_server_url="https://mcp.hail.so")
    assert await v.verify_token("") is None


@pytest.mark.asyncio
async def test_pass_through_verifier_is_pass_through_no_signature_check():
    """Garbage-shaped tokens still pass — signature/issuer/exp is the API's
    job, not MCP's. This guards against accidentally adding validation here."""
    v = PassThroughVerifier(resource_server_url="https://mcp.hail.so")
    assert await v.verify_token("not-even-jwt-shaped") is not None
    assert await v.verify_token("aaa.bbb.malformed") is not None
