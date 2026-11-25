"""Mode-A/B + E.164 validation on CallCreate."""

from __future__ import annotations

import pytest
from hail.models import CallCreate, LLMConfig
from pydantic import ValidationError


def test_call_create_mode_a_valid() -> None:
    body = CallCreate(
        to="+14155551234", system_prompt="be polite", recipient_consent=True
    )
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["to"] == "+14155551234"
    assert dumped["system_prompt"] == "be polite"
    assert "llm" not in dumped


def test_call_create_mode_b_valid() -> None:
    body = CallCreate(
        to="+14155551234",
        recipient_consent=True,
        llm=LLMConfig(
            base_url="https://api.openai.com/v1",
            api_key="sk-x",
            model="gpt-4o-mini",
        ),
    )
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["llm"]["model"] == "gpt-4o-mini"
    assert "system_prompt" not in dumped


def test_call_create_accepts_both_modes() -> None:
    """Prompt + BYO endpoint together: both survive onto the wire body."""
    body = CallCreate(
        to="+14155551234",
        system_prompt="be polite",
        recipient_consent=True,
        llm=LLMConfig(
            base_url="https://api.openai.com/v1",
            api_key="sk-x",
            model="gpt-4o-mini",
        ),
    )
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["system_prompt"] == "be polite"
    assert dumped["llm"]["model"] == "gpt-4o-mini"


def test_call_create_rejects_neither_mode() -> None:
    with pytest.raises(ValidationError) as exc:
        CallCreate(to="+14155551234", recipient_consent=True)
    assert "either system_prompt" in str(exc.value)


def test_call_create_rejects_non_e164() -> None:
    with pytest.raises(ValidationError) as exc:
        CallCreate(to="not-a-number", system_prompt="hi", recipient_consent=True)
    assert "E.164" in str(exc.value)


def test_call_create_from_alias_python_kwarg() -> None:
    """``from_=`` should serialize to ``"from"`` on the wire."""
    body = CallCreate(
        to="+14155551234",
        from_="+15550001111",
        system_prompt="hi",
        recipient_consent=True,
    )
    dumped = body.model_dump(by_alias=True, exclude_none=True)
    assert dumped["from"] == "+15550001111"
    assert "from_" not in dumped


def test_phone_number_without_source_defaults_purchased():
    from hail.models import PhoneNumberResponse

    base = {
        "id": "00000000-0000-0000-0000-000000000001",
        "e164": "+14155550100",
        "country_code": "US",
        "number_type": "local",
        "capabilities": ["voice"],
        "provisioning_state": "active",
        "is_dedicated": True,
    }
    old = PhoneNumberResponse.model_validate(base)
    assert (old.source, old.sip_connection_id) == ("purchased", None)
    new = PhoneNumberResponse.model_validate(
        {
            **base,
            "source": "connected",
            "sip_connection_id": "00000000-0000-0000-0000-000000000002",
        }
    )
    assert new.source == "connected"
    assert str(new.sip_connection_id).endswith("0002")
