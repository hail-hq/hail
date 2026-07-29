"""VoiceConfig language enum + stt selector validation."""

from __future__ import annotations

import pytest
from hailhq.core.provider_config import STTParams
from hailhq.core.schemas import VoiceConfig
from pydantic import ValidationError


def test_supported_language_accepted() -> None:
    assert VoiceConfig(language="da").language == "da"


def test_unsupported_language_rejected() -> None:
    with pytest.raises(ValidationError):
        VoiceConfig(language="ka")  # excluded: Whisper-only STT
    with pytest.raises(ValidationError):
        VoiceConfig(language="xx")


def test_stt_defaults_to_auto_and_accepts_speechmatics() -> None:
    assert VoiceConfig().stt == "auto"
    assert VoiceConfig(stt="speechmatics").stt == "speechmatics"
    with pytest.raises(ValidationError):
        VoiceConfig(stt="whisper")


def test_openapi_schema_exposes_language_enum() -> None:
    schema = VoiceConfig.model_json_schema()
    prop = schema["properties"]["language"]
    # pydantic renders Optional[Literal[...]] as anyOf [enum, null]
    enums = [e for e in prop.get("anyOf", []) if "enum" in e]
    assert enums and len(enums[0]["enum"]) == 39


def test_stt_params_accept_speechmatics() -> None:
    assert STTParams(provider="speechmatics").provider == "speechmatics"
