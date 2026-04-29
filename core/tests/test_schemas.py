from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from hailhq.core.schemas import (
    CallCreate,
    LLMConfig,
    VoiceConfig,
    parse_resource_id,
)


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


def test_call_create_rejects_prompt_and_llm_together():
    with pytest.raises(ValidationError, match="mutually exclusive"):
        CallCreate(
            to="+14155551234",
            system_prompt="Hi",
            llm=LLMConfig(base_url="https://x.example/v1", api_key="k", model="m"),
        )


def test_voice_config_defaults():
    cfg = VoiceConfig()
    assert cfg.stt == "deepgram"
    assert cfg.tts == "elevenlabs"
    assert cfg.vad == "silero"


def test_parse_resource_id_call_happy_path():
    u = uuid4()
    rtype, rid = parse_resource_id(f"call:{u}")
    assert rtype == "call"
    assert rid == u
    assert isinstance(rid, UUID)


def test_parse_resource_id_missing_colon():
    with pytest.raises(ValueError, match="missing ':'"):
        parse_resource_id("nocolon")


def test_parse_resource_id_unsupported_type():
    with pytest.raises(ValueError, match=r"unsupported resource type 'sms'"):
        parse_resource_id(f"sms:{uuid4()}")


def test_parse_resource_id_empty_type():
    with pytest.raises(ValueError, match="missing resource type"):
        parse_resource_id(":")


def test_parse_resource_id_empty_id():
    with pytest.raises(ValueError, match="missing resource id"):
        parse_resource_id("call:")


def test_parse_resource_id_bad_uuid():
    with pytest.raises(ValueError, match="invalid uuid"):
        parse_resource_id("call:not-a-uuid")
