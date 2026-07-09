"""Per-layer param schemas + the dedicated provider-key cipher."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.provider_config import (
    LLMParams,
    STTParams,
    TTSParams,
    last4,
    load_org_provider_configs,
    provider_cipher,
)
from hailhq.core.secret_cipher import SecretKeyMissing, generate_key


def test_llm_params_openai_compatible_requires_base_url() -> None:
    with pytest.raises(ValidationError):
        LLMParams(provider="openai-compatible", model="gpt-5.4-mini")
    ok = LLMParams(
        provider="openai-compatible",
        base_url="https://api.openai.com/v1",
        model="gpt-5.4-mini",
    )
    assert ok.base_url == "https://api.openai.com/v1"


def test_llm_params_native_providers_forbid_base_url() -> None:
    with pytest.raises(ValidationError):
        LLMParams(
            provider="anthropic",
            base_url="https://example.com",
            model="claude-sonnet-4-6",
        )
    ok = LLMParams(provider="google", model="gemini-3-flash")
    assert ok.base_url is None


def test_tts_and_stt_params() -> None:
    t = TTSParams(provider="elevenlabs", voice_id="694f9389", model=None)
    assert t.voice_id == "694f9389"
    with pytest.raises(ValidationError):
        TTSParams(provider="deepgram")  # wrong layer's provider
    s = STTParams(provider="deepgram", model="nova-3")
    assert s.model == "nova-3"
    with pytest.raises(ValidationError):
        STTParams(provider="deepgram", voice_id="x")  # extra forbid


def test_provider_cipher_requires_dedicated_key(monkeypatch) -> None:
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    with pytest.raises(SecretKeyMissing):
        provider_cipher()
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    cipher = provider_cipher()
    assert cipher.decrypt(cipher.encrypt("sk-test-123")) == "sk-test-123"


def test_last4() -> None:
    assert last4("sk-abcdef4F2A") == "4F2A"
    assert last4("abc") == "abc"


async def test_load_org_provider_configs(async_session, monkeypatch) -> None:
    org_id = uuid.uuid4()
    other_org = uuid.uuid4()
    async_session.add_all(
        [
            OrgProviderConfig(organization_id=org_id, layer="tts", provider="cartesia"),
            OrgProviderConfig(
                organization_id=org_id, layer="llm", provider="anthropic"
            ),
            OrgProviderConfig(
                organization_id=other_org, layer="stt", provider="deepgram"
            ),
        ]
    )
    await async_session.commit()

    got = await load_org_provider_configs(async_session, org_id)
    assert set(got) == {"tts", "llm"}
    assert got["llm"].provider == "anthropic"
