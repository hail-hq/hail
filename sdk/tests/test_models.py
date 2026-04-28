"""Mode-A/B + E.164 validation on CallCreate."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hail.models import CallCreate, LLMConfig


def test_call_create_mode_a_valid() -> None:
    body = CallCreate(to="+14155551234", system_prompt="be polite")
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["to"] == "+14155551234"
    assert dumped["system_prompt"] == "be polite"
    assert "llm" not in dumped


def test_call_create_mode_b_valid() -> None:
    body = CallCreate(
        to="+14155551234",
        llm=LLMConfig(
            base_url="https://api.openai.com/v1",
            api_key="sk-x",
            model="gpt-4o-mini",
        ),
    )
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["llm"]["model"] == "gpt-4o-mini"
    assert "system_prompt" not in dumped


def test_call_create_rejects_both_modes() -> None:
    with pytest.raises(ValidationError) as exc:
        CallCreate(
            to="+14155551234",
            system_prompt="be polite",
            llm=LLMConfig(
                base_url="https://api.openai.com/v1",
                api_key="sk-x",
                model="gpt-4o-mini",
            ),
        )
    assert "mutually exclusive" in str(exc.value)


def test_call_create_rejects_neither_mode() -> None:
    with pytest.raises(ValidationError) as exc:
        CallCreate(to="+14155551234")
    assert "either system_prompt or" in str(exc.value)


def test_call_create_rejects_non_e164() -> None:
    with pytest.raises(ValidationError) as exc:
        CallCreate(to="not-a-number", system_prompt="hi")
    assert "E.164" in str(exc.value)


def test_call_create_from_alias_python_kwarg() -> None:
    """``from_=`` should serialize to ``"from"`` on the wire."""
    body = CallCreate(
        to="+14155551234",
        from_="+15550001111",
        system_prompt="hi",
    )
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["from"] == "+15550001111"
    assert "from_" not in dumped
