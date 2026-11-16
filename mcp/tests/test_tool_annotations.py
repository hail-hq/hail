"""Every registered MCP tool carries explicit annotation hints.

A new tool fails this test until it is classified below, so no tool ships
with a default (and misleading) hint set.
"""

from __future__ import annotations

import re
import unicodedata

# Read-only tools: never change anything.
_READ_ONLY = {
    "get_call",
    "list_calls",
    "get_sms",
    "list_sms",
    "get_email",
    "list_emails",
    "get_email_raw",
    "get_email_attachment",
    "get_email_events",
    "get_email_stats",
    "get_events",
    "list_agents",
    "list_contacts",
    "lookup_contact",
    "list_email_domains",
    "whoami",
    "get_agent",
    "list_numbers",
    "list_number_regions",
    "get_number",
    "quote_numbers",
    "get_verification_requirements",
    "list_verifications",
    "get_verification",
    "get_sms_application",
    "preview_sms_application",
    "check_verification",
}
# Read-only tools that talk to outside systems.
_READ_ONLY_OPEN_WORLD = {
    "check_verification",
    "get_sms_application",
    "preview_sms_application",
    "quote_numbers",
    "get_verification_requirements",
    "list_verifications",
    "get_verification",
}

# Destructive tools. acquire_number spends money, so clients ask a human.
# submit_verification cannot be undone; cancel_verification discards a draft.
_DESTRUCTIVE = {
    "release_number",
    "delete_agent",
    "acquire_number",
    "submit_verification",
    "amend_sms_application",
    "cancel_verification",
}

# Tools that reach people or carriers outside Hail.
_OPEN_WORLD_WRITES = {
    "place_call",
    "send_sms",
    "send_email",
    "release_number",
    "acquire_number",
    "submit_verification",
    "amend_sms_application",
    "cancel_verification",
    "confirm_verification",
    # Uses the workspace's daily document reads, which cost money.
    "check_verification_documents",
}

_EXPECTED_COUNT = 45


async def test_tool_count(tools_by_name) -> None:
    assert len(tools_by_name) == _EXPECTED_COUNT


async def test_every_tool_has_explicit_hints_and_title(tools_by_name) -> None:
    for name, tool in tools_by_name.items():
        a = tool.annotations
        assert a is not None, name
        wire = a.model_dump(by_alias=True)
        for hint in (
            "readOnlyHint",
            "destructiveHint",
            "idempotentHint",
            "openWorldHint",
        ):
            assert wire[hint] is not None, f"{name}.{hint} not set"
        assert tool.title, f"{name} has no title"
        # Directory listings read annotations.title, hosts read Tool.title.
        assert a.title == tool.title, f"{name} annotations.title differs"


def _schema_descriptions(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "description" and isinstance(item, str):
                yield item
            else:
                yield from _schema_descriptions(item)
    elif isinstance(value, list):
        for item in value:
            yield from _schema_descriptions(item)


async def test_public_descriptions_state_contracts_without_model_instructions(
    tools_by_name,
):
    """Directory descriptions document tools, rather than direct the model."""
    instructions = re.compile(
        r"\b(?:you|your|never|prefer|ask the user|ask the person|tell the user|"
        r"show the user|do not|don't|call this|use this|then call|first call|"
        r"before calling|poll every|should|ask|pass|make sure|remember|always|"
        r"avoid|prefer to|be sure)\b|\bloop:",
        re.IGNORECASE,
    )
    cross_tool_call = re.compile(
        r"\b(?:call|use|run|invoke|read)\s+`*(?:"
        + "|".join(re.escape(name) for name in tools_by_name)
        + r")\b",
        re.IGNORECASE,
    )
    for name, tool in tools_by_name.items():
        assert tool.description and tool.description.strip(), name
        for text in [tool.description, *_schema_descriptions(tool.input_schema)]:
            assert not instructions.search(text), (name, instructions.search(text))
            assert not cross_tool_call.search(text), (
                name,
                cross_tool_call.search(text),
            )
            assert all(
                c in "\n\t" or unicodedata.category(c) not in {"Cc", "Cf"} for c in text
            ), name


async def test_submit_verification_params_have_schema_types(tools_by_name) -> None:
    props = tools_by_name["submit_verification"].input_schema["properties"]
    assert props["fields"]["type"] == "object"
    assert props["documents"]["type"] == "object"
    assert props["files"]["type"] == "array"
    assert props["files"]["maxItems"] == 20
    upload, inline = props["files"]["items"]["anyOf"]
    assert upload["required"] == ["slot", "upload_id"]
    assert inline["properties"]["content_type"]["enum"] == [
        "image/jpeg",
        "image/png",
        "application/pdf",
    ]
    address, null = props["address"]["anyOf"]
    assert null == {"type": "null"}
    assert address["type"] == "object"
    assert set(address["required"]) == {
        "customer_name",
        "street",
        "city",
        "region",
        "postal_code",
        "country_code",
    }


async def test_read_only_set_is_exact(tools_by_name) -> None:
    got = {n for n, t in tools_by_name.items() if t.annotations.read_only_hint is True}
    assert got == _READ_ONLY


async def test_read_only_tools_are_idempotent_not_destructive(tools_by_name) -> None:
    for name in _READ_ONLY:
        a = tools_by_name[name].annotations
        assert a.destructive_hint is False, name
        assert a.idempotent_hint is True, name
        assert a.open_world_hint is (name in _READ_ONLY_OPEN_WORLD), name


async def test_destructive_set_is_exact(tools_by_name) -> None:
    got = {
        n for n, t in tools_by_name.items() if t.annotations.destructive_hint is True
    }
    assert got == _DESTRUCTIVE
    # Not idempotent: a repeat submits again or fails.
    not_idempotent = {"submit_verification", "cancel_verification"}
    for name in _DESTRUCTIVE:
        a = tools_by_name[name].annotations
        assert a.idempotent_hint is (name not in not_idempotent), name
        assert a.read_only_hint is False, name


async def test_open_world_writes(tools_by_name) -> None:
    got = {
        n
        for n, t in tools_by_name.items()
        if t.annotations.open_world_hint is True
        and t.annotations.read_only_hint is False
    }
    assert got == _OPEN_WORLD_WRITES
