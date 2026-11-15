"""Limits on the server metadata sent in ``initialize``."""

from __future__ import annotations

import re
from pathlib import Path

from hailhq.mcp import metadata, tools

# Claude Code cuts each tool description and the instructions at 2 KB.
CLIENT_LIMIT_BYTES = 2048


def test_instructions_fit_client_limit() -> None:
    assert len(metadata.INSTRUCTIONS.encode()) <= CLIENT_LIMIT_BYTES


def test_instructions_name_only_real_tools() -> None:
    source = Path(tools.__file__).read_text()
    registered = set(re.findall(r'name="([a-z_]+)"', source))
    named = set(re.findall(r"\b([a-z]+_[a-z_]+)\b", metadata.INSTRUCTIONS))
    params = {"recipient_consent", "consent_source", "idempotency_key", "next_cursor"}
    assert named - params <= registered


def test_website_url_follows_site_setting(monkeypatch) -> None:
    monkeypatch.setattr("hailhq.core.config.settings.hail_base_url", "https://x.test/")
    assert metadata.website_url() == "https://x.test/mcp"
    monkeypatch.setattr("hailhq.core.config.settings.hail_base_url", "")
    assert metadata.website_url() is None
