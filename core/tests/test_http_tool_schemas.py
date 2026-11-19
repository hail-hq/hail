"""HttpToolIn and the http_tools lists on AgentCreate / AgentUpdate."""

from __future__ import annotations

import pytest
from hailhq.core.agent_tools import email_caller, hail_help, transfer_call
from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.schemas import (
    RESERVED_TOOL_NAMES,
    AgentCreate,
    AgentUpdate,
    HttpToolIn,
)
from pydantic import ValidationError


def _tool(**over):
    base = {
        "name": "lookup_order",
        "description": "Find an order by its number.",
        "url": "https://api.example.com/orders",
        "parameters": {
            "type": "object",
            "properties": {
                "order_number": {"type": "string", "description": "The number."}
            },
            "required": ["order_number"],
        },
    }
    base.update(over)
    return base


def test_reserved_names_cover_every_built_in_tool() -> None:
    names = {s.name for s in all_tools()}
    names |= {
        hail_help.SPEC.name,
        transfer_call.CANCEL_SPEC.name,
        email_caller.SPEC.name,
    }
    assert names <= RESERVED_TOOL_NAMES


def test_defaults() -> None:
    tool = HttpToolIn.model_validate(_tool())
    assert tool.timeout_seconds == 5
    assert tool.headers is None
    assert tool.wait_message is None


def test_parameters_default_is_an_empty_object() -> None:
    raw = _tool()
    del raw["parameters"]
    assert HttpToolIn.model_validate(raw).parameters == {
        "type": "object",
        "properties": {},
    }


@pytest.mark.parametrize(
    "over",
    [
        {"name": "1abc"},
        {"name": "has space"},
        {"name": "end_call"},
        {"name": "hail_settings_help"},
        {"name": "x" * 65},
        {"description": "   "},
        {"url": "http://api.example.com/orders"},
        {"url": "https:///no-host"},
        {"url": "https://user:pw@api.example.com/"},
        {"url": "https://api.example.com/#frag"},
        {"timeout_seconds": 0},
        {"timeout_seconds": 11},
        {"wait_message": "x" * 201},
        {"parameters": {"type": "string"}},
        {"parameters": {"type": "object", "properties": {"a": {"type": "date"}}}},
        {"parameters": {"type": "object", "properties": {"a": {"$ref": "#/x"}}}},
        {
            "parameters": {
                "type": "object",
                "properties": {"bad name": {"type": "string"}},
            }
        },
        {"parameters": {"type": "object", "properties": {}, "required": ["missing"]}},
        {"headers": {"X-Hail-Signature": "forged"}},
        {"headers": {"Host": "evil"}},
        {"headers": {"Content-Type": "text/plain"}},
        {"headers": {"user-agent": "acme"}},
        {"headers": {"Bad Name": "x"}},
        {"headers": {"Authorization": "a\r\nInjected: yes"}},
        {"headers": {f"H{i}": "v" for i in range(11)}},
        {"unknown": True},
        {"parameters": {"type": "object", "additionalProperties": False}},
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": ["string", "null"]}},
            }
        },
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string", "pattern": "x"}},
            }
        },
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer", "enum": [1, 2]}},
            }
        },
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "integer", "minimum": "1"}},
            }
        },
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string", "maxLength": -1}},
            }
        },
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string", "description": 5}},
            }
        },
        {
            "parameters": {
                "type": "object",
                "properties": {"a": {"type": "string", "items": {"type": "string"}}},
            }
        },
    ],
)
def test_refuses_bad_input(over) -> None:
    with pytest.raises(ValidationError):
        HttpToolIn.model_validate(_tool(**over))


def test_parameters_too_deep() -> None:
    node = {"type": "string"}
    for _ in range(5):
        node = {"type": "object", "properties": {"n": node}}
    with pytest.raises(ValidationError, match="deeper"):
        HttpToolIn.model_validate(_tool(parameters=node))


def test_accepts_nested_arrays_enums_and_headers() -> None:
    tool = HttpToolIn.model_validate(
        _tool(
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "string", "enum": ["a", "b"]},
                    },
                    "size": {"type": "integer", "minimum": 1, "maximum": 99},
                },
            },
            headers={"Authorization": "Bearer sk_live_example"},
            wait_message="  One moment.  ",
        )
    )
    assert tool.headers == {"Authorization": "Bearer sk_live_example"}
    assert tool.wait_message == "One moment."


def test_agent_create_caps_and_unique_names() -> None:
    base = {"name": "Desk", "system_prompt": "Help."}
    AgentCreate.model_validate(base | {"http_tools": [_tool()]})
    with pytest.raises(ValidationError, match="two tools named"):
        AgentCreate.model_validate(base | {"http_tools": [_tool(), _tool()]})
    many = [_tool(name=f"tool_{i}") for i in range(11)]
    with pytest.raises(ValidationError):
        AgentCreate.model_validate(base | {"http_tools": many})


def test_agent_update_keeps_none_and_checks_names() -> None:
    assert AgentUpdate.model_validate({}).http_tools is None
    assert AgentUpdate.model_validate({"http_tools": []}).http_tools == []
    with pytest.raises(ValidationError, match="two tools named"):
        AgentUpdate.model_validate({"http_tools": [_tool(), _tool()]})


def test_uppercase_scheme_is_stored_lowercase() -> None:
    tool = HttpToolIn.model_validate(_tool(url="HTTPS://api.example.com/Orders"))
    assert tool.url == "https://api.example.com/Orders"
