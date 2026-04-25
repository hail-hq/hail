import pytest
from pydantic import ValidationError

from hailhq.core.schemas import CallCreate, LLMConfig, VoiceConfig


def test_call_create_minimal_valid():
    req = CallCreate(to="+14155551234", system_prompt="Hi")
    assert req.to == "+14155551234"
    assert req.system_prompt == "Hi"
    assert req.llm is None


def test_call_create_with_byo_endpoint():
    req = CallCreate(
        to="+14155551234",
        llm=LLMConfig(base_url="https://x.example/v1", api_key="k", model="m"),
    )
    assert req.llm is not None
    assert req.llm.base_url == "https://x.example/v1"


def test_call_create_rejects_non_e164():
    with pytest.raises(ValidationError):
        CallCreate(to="4155551234", system_prompt="Hi")


def test_call_create_requires_prompt_or_llm():
    with pytest.raises(ValidationError):
        CallCreate(to="+14155551234")


def test_voice_config_defaults():
    cfg = VoiceConfig()
    assert cfg.stt == "deepgram"
    assert cfg.tts == "elevenlabs"
    assert cfg.vad == "silero"
