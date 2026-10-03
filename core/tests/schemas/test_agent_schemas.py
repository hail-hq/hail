"""Agent request/response shapes and the agent_id hook on CallCreate."""

from __future__ import annotations

import uuid
from typing import get_args

import pytest
from hailhq.core.schemas import (
    AgentCreate,
    AgentUpdate,
    CallCreate,
    PhoneNumberResponse,
    PhoneNumberRoutingUpdate,
    WebhookEventType,
)
from pydantic import ValidationError


def test_agent_create_defaults():
    a = AgentCreate(name="Front desk", system_prompt="Be kind.")
    assert a.ai_disclosure is True
    assert a.ai_disclosure_line is None
    assert a.first_message is None
    assert a.tools is None
    assert a.max_duration_seconds is None
    assert a.sms_enabled is True
    assert a.status == "live"
    assert a.voice_config.model_dump()  # a VoiceConfig, not a bare dict


def test_agent_create_rejects_empty_name_and_long_line():
    with pytest.raises(ValidationError):
        AgentCreate(name="  ", system_prompt="x")
    with pytest.raises(ValidationError):
        AgentCreate(name="a", system_prompt="x", ai_disclosure_line="y" * 2000)
    with pytest.raises(ValidationError):
        AgentCreate(name="a", system_prompt="x", max_duration_seconds=10)


def test_agent_update_is_all_optional():
    u = AgentUpdate()
    assert u.model_dump(exclude_unset=True) == {}
    u = AgentUpdate(first_message=None)
    assert u.model_dump(exclude_unset=True) == {"first_message": None}


def test_call_create_accepts_agent_id_alone():
    c = CallCreate(to="+14155550100", recipient_consent=True, agent_id=uuid.uuid4())
    assert c.system_prompt is None
    with pytest.raises(ValidationError):
        CallCreate(to="+14155550100", recipient_consent=True)


def test_routing_update_distinguishes_unset_from_null():
    r = PhoneNumberRoutingUpdate(voice_agent_id=None)
    assert r.model_dump(exclude_unset=True) == {"voice_agent_id": None}
    with pytest.raises(ValidationError):
        PhoneNumberRoutingUpdate()


def test_phone_number_response_routing_fields():
    fields = PhoneNumberResponse.model_fields
    assert {"voice_agent_id", "sms_agent_id", "inbound_registered"} <= set(fields)


def test_call_received_event_type():
    assert "call.received" in get_args(WebhookEventType)
