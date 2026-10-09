"""Ids go into URL paths. A hostile id must never reach the wire.

httpx resolves dot segments, so ``../numbers/<uuid>`` would turn a call for a
verification into a DELETE on a number and skip its confirm step.
"""

from __future__ import annotations

import inspect
import json
from uuid import uuid4

import pytest
import respx
from hailhq.mcp import tools
from hailhq.mcp.hail_client import HailClient, _path_id

_BASE_URL = "http://hail-test"
_UUID = str(uuid4())

_HOSTILE = [
    f"../numbers/{_UUID}",
    "x/../../agents/y",
    "..",
    ".",
    "",
    "a/b",
    "a\\b",
    "x?admin=1",
    "x#frag",
    "%2e%2e",
    "a b",
    "a\tb",
    "a\nb",
    "a\x00b",
]


@pytest.fixture()
async def client() -> HailClient:
    c = HailClient(base_url=_BASE_URL, api_key="test-key")
    try:
        yield c
    finally:
        await c.aclose()


# --------------------------------------------------------------------------- #
# _path_id
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", [_UUID, "abc123", "email_01HX-9", "a.b", "a~b", "é"])
def test_path_id_accepts_normal_ids(value: str) -> None:
    out = _path_id(value, name="thing_id")
    assert "/" not in out
    assert out == __import__("urllib.parse").parse.quote(value, safe="")


def test_path_id_quotes_odd_but_allowed_characters() -> None:
    assert _path_id("a:b@c+d") == "a%3Ab%40c%2Bd"


@pytest.mark.parametrize("value", _HOSTILE)
def test_path_id_rejects_hostile_ids(value: str) -> None:
    with pytest.raises(ValueError) as exc:
        _path_id(value, name="thing_id")
    assert "thing_id" in str(exc.value)
    assert value.strip() == "" or value not in str(exc.value)


def test_path_id_rejects_non_strings() -> None:
    with pytest.raises(ValueError):
        _path_id(123, name="thing_id")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Every HailClient method that puts an id in a path
# --------------------------------------------------------------------------- #

# Methods whose *_id parameter goes in a JSON body, not a path.
_BODY_ID_METHODS = {"place_call", "send_sms", "send_email", "acquire_number"}


def _id_methods() -> list[tuple[str, list[str]]]:
    found = []
    for name, fn in inspect.getmembers(HailClient, inspect.iscoroutinefunction):
        if name.startswith("_") or name in _BODY_ID_METHODS:
            continue
        params = [
            p
            for p in inspect.signature(fn).parameters
            if p.endswith("_id") and p not in {"voice_agent_id", "sms_agent_id"}
        ]
        if params:
            found.append((name, params))
    return found


def test_the_id_method_list_is_the_expected_one() -> None:
    assert {n for n, _ in _id_methods()} == {
        "get_call",
        "route_number",
        "get_agent",
        "update_agent",
        "delete_agent",
        "get_number",
        "delete_number",
        "get_sms",
        "get_verification",
        "cancel_verification",
        "get_email",
        "get_email_raw",
        "get_email_attachment",
        "get_email_events",
    }


@respx.mock
@pytest.mark.parametrize("hostile", _HOSTILE)
@pytest.mark.parametrize("method,params", _id_methods())
async def test_hostile_id_raises_before_sending(
    client: HailClient, method: str, params: list[str], hostile: str
) -> None:
    catch_all = respx.route().mock(return_value=__import__("httpx").Response(500))
    for target in params:
        kwargs = {p: (hostile if p == target else _UUID) for p in params}
        with pytest.raises(ValueError):
            await getattr(client, method)(**kwargs)
    assert catch_all.call_count == 0


# --------------------------------------------------------------------------- #
# Tool functions
# --------------------------------------------------------------------------- #

_TOOL_CASES = [
    ("cancel_verification", "verification_id", {}),
    ("get_verification", "verification_id", {}),
    ("release_number", "number_id", {"confirm_e164": "+15550001111"}),
    ("get_number", "number_id", {}),
    ("route_number", "number_id", {"clear_voice": True}),
    ("delete_agent", "agent_id", {"confirm_name": "x"}),
    ("get_agent", "agent_id", {}),
    ("update_agent", "agent_id", {"name": "x"}),
    (
        "acquire_number",
        "quote_id",
        {"country_code": "us", "number_type": "local", "confirm_total_cents": 1},
    ),
]


@respx.mock
@pytest.mark.parametrize("hostile", _HOSTILE)
@pytest.mark.parametrize("tool,param,extra", _TOOL_CASES)
async def test_uuid_tools_refuse_hostile_ids_without_a_call(
    client: HailClient, tool: str, param: str, extra: dict, hostile: str
) -> None:
    catch_all = respx.route().mock(return_value=__import__("httpx").Response(500))
    result = await getattr(tools, tool)(client=client, **{param: hostile}, **extra)
    assert catch_all.call_count == 0
    assert result == {"error": f"{param} must be a UUID"}
    assert hostile not in json.dumps(result) or hostile == ""


_FREE_ID_CASES = [
    ("get_call", "call_id", {}),
    ("get_sms", "sms_id", {}),
    ("get_email", "email_id", {}),
    ("get_email_raw", "email_id", {}),
    ("get_email_events", "email_id", {}),
    ("get_email_attachment", "email_id", {"attachment_id": "att1"}),
    ("get_email_attachment", "attachment_id", {"email_id": "e1"}),
]


@respx.mock
@pytest.mark.parametrize("hostile", _HOSTILE)
@pytest.mark.parametrize("tool,param,extra", _FREE_ID_CASES)
async def test_other_id_tools_refuse_hostile_ids_without_a_call(
    client: HailClient, tool: str, param: str, extra: dict, hostile: str
) -> None:
    catch_all = respx.route().mock(return_value=__import__("httpx").Response(500))
    result = await getattr(tools, tool)(client=client, **{param: hostile}, **extra)
    assert catch_all.call_count == 0
    assert result == {"error": f"{param} is not a valid id"}
