"""Registry → LiveKit adaptation: opt-out, degradation, error isolation."""

from __future__ import annotations

import dataclasses
import json
import uuid
from unittest.mock import AsyncMock

import hailhq.voicebot.tools as tools_mod
import pytest
from hailhq.core.agent_tools import http_tool
from hailhq.core.config import settings
from hailhq.core.models import Agent, AgentHttpTool
from hailhq.core.secret_cipher import SecretCipher, generate_key
from hailhq.voicebot.tools import (
    SPOKEN_TOOL_FAILURE,
    _make_handler,
    build_agent_tools,
)

# The tools every call keeps, in build order.
EVERY_CALL = ["end_call", "send_dtmf", "hail_settings_help"]


class FakeRunContext:
    def __init__(self):
        self.waited = False
        self.interruptions_allowed = True

    def disallow_interruptions(self):
        self.interruptions_allowed = False

    async def wait_for_playout(self):
        self.waited = True


def _spec(name="boom", tier="read_only", execute=None, is_available=None):
    from hailhq.core.agent_tools.spec import ToolSpec

    async def _avail(_org, _session):
        return True

    async def _default_execute(_ctx, _args):
        return "ok"

    return ToolSpec(
        name=name,
        description="d",
        parameters={"type": "object", "properties": {}, "required": []},
        risk_tier=tier,
        is_available=is_available or _avail,
        execute=execute or _default_execute,
    )


def _tctx():
    from hailhq.core.agent_tools.spec import ToolContext

    return ToolContext(
        call_id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        api=None,
        hangup=None,
        send_dtmf=None,
    )


async def test_empty_opt_out_keeps_only_every_call_tools():
    """`tools: []` turns off every tool but the ones every call keeps
    (ToolSpec.on_every_call): a call can always hang up."""
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "tools": []},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    try:
        assert [t.info.name for t in tools] == EVERY_CALL
    finally:
        if api is not None:
            await api.aclose()


async def test_a_tools_list_keeps_every_call_tools(db):
    tools, api = await build_agent_tools(
        {
            "organization_id": str(uuid.uuid4()),
            "tools": ["send_email", "send_sms", "list_contacts"],
        },
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    try:
        assert set(EVERY_CALL) <= {t.info.name for t in tools}
    finally:
        if api is not None:
            await api.aclose()


async def test_missing_organization_id_keeps_only_every_call_tools():
    """No org tools without an org, but the call can still hang up."""
    tools, api = await build_agent_tools(
        {}, call_id=uuid.uuid4(), hangup=None, send_dtmf=None
    )
    assert [t.info.name for t in tools] == EVERY_CALL
    assert api is None


async def test_malformed_organization_id_keeps_only_every_call_tools():
    tools, api = await build_agent_tools(
        {"organization_id": "not-a-uuid"},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    assert [t.info.name for t in tools] == EVERY_CALL
    assert api is None


async def test_malformed_tools_field_keeps_only_every_call_tools():
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "tools": "send_sms"},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    assert [t.info.name for t in tools] == EVERY_CALL
    assert api is None


async def test_every_call_tools_open_no_database(monkeypatch):
    """`tools: []` needs no availability check, so a slow database does not
    delay the call."""

    def no_db():
        raise AssertionError("session_scope opened")

    monkeypatch.setattr(tools_mod, "session_scope", no_db)
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "tools": []},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    assert [t.info.name for t in tools] == EVERY_CALL
    assert api is None


async def test_wrapper_isolates_tool_exceptions():
    async def explode(_ctx, _args):
        raise RuntimeError("kaboom")

    handler = _make_handler(_spec(execute=explode), _tctx())
    result = await handler({}, FakeRunContext())
    assert result == SPOKEN_TOOL_FAILURE


async def test_session_control_waits_for_playout():
    seen = []

    async def record(_ctx, _args):
        seen.append("executed")
        return "bye"

    rc = FakeRunContext()
    handler = _make_handler(_spec(tier="session_control", execute=record), _tctx())
    result = await handler({}, rc)
    assert rc.waited is True
    assert result == "bye"


async def test_read_only_does_not_wait():
    rc = FakeRunContext()
    handler = _make_handler(_spec(tier="read_only"), _tctx())
    await handler({}, rc)
    assert rc.waited is False


async def test_availability_failure_does_not_poison_session(db, monkeypatch):
    """A DB error in one is_available check must not disable later tools.

    Without a rollback the shared session's transaction stays DEACTIVE
    after the first failure, so the second check's execute() raises
    PendingRollbackError and a genuinely-available tool is silently lost.
    """
    from livekit.agents.llm.tool_context import get_raw_function_info
    from sqlalchemy import text

    async def broken_avail(_org, session):
        await session.execute(text("SELECT * FROM nonexistent_table_xyz"))
        return True

    async def healthy_avail(_org, session):
        await session.execute(text("SELECT 1"))
        return True

    specs = (
        _spec(name="broken_tool", is_available=broken_avail),
        _spec(name="healthy_tool", is_available=healthy_avail),
    )
    monkeypatch.setattr(tools_mod, "call_tools", lambda: specs)

    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4())},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    try:
        assert [get_raw_function_info(t).name for t in tools] == ["healthy_tool"]
    finally:
        if api is not None:
            await api.aclose()


async def test_bind_shapes_transfer_call(db) -> None:
    meta = {
        "organization_id": str(uuid.uuid4()),
        "tools": None,
        "handover_targets": [
            {"contact_id": str(uuid.uuid4()), "label": "Sam", "note": "Billing"}
        ],
    }
    tools, api = await build_agent_tools(
        meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None, bridge=AsyncMock()
    )
    try:
        names = [t.info.name for t in tools]
        assert "transfer_call" in names and "cancel_transfer" in names
        (tool,) = [t for t in tools if t.info.name == "transfer_call"]
        assert "Sam (Billing)" in tool.info.raw_schema["description"]
    finally:
        if api is not None:
            await api.aclose()


async def test_transfer_call_hidden_without_targets(db) -> None:
    meta = {
        "organization_id": str(uuid.uuid4()),
        "tools": None,
        "handover_targets": [],
    }
    tools, api = await build_agent_tools(
        meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None, bridge=AsyncMock()
    )
    try:
        names = [t.info.name for t in tools]
        assert "transfer_call" not in names and "cancel_transfer" not in names
    finally:
        if api is not None:
            await api.aclose()


async def test_cancel_comes_with_transfer_call_in_a_tools_list(db) -> None:
    """A `tools` list names transfer_call only; cancel_transfer comes with it."""
    meta = {
        "organization_id": str(uuid.uuid4()),
        "tools": ["transfer_call"],
        "handover_targets": [
            {"contact_id": str(uuid.uuid4()), "label": "Sam", "note": "Billing"}
        ],
    }
    tools, api = await build_agent_tools(
        meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None, bridge=AsyncMock()
    )
    try:
        assert sorted(t.info.name for t in tools) == sorted(
            [*EVERY_CALL, "transfer_call", "cancel_transfer"]
        )
    finally:
        if api is not None:
            await api.aclose()


async def test_session_tool_that_must_not_wait_runs_at_once():
    """cancel_transfer stops the ring at once, not after the agent's
    sentence, which may take seconds while the contact can pick up."""
    from hailhq.core.agent_tools import transfer_call

    assert transfer_call.CANCEL_SPEC.waits_for_speech is False
    rc = FakeRunContext()
    await _make_handler(transfer_call.CANCEL_SPEC, _tctx())({}, rc)
    assert rc.waited is False
    rc = FakeRunContext()
    await _make_handler(_spec(tier="session_control"), _tctx())({}, rc)
    assert rc.waited is True


async def test_end_call_keeps_the_call_when_they_talk_over_the_goodbye():
    from hailhq.voicebot.tools import STILL_TALKING

    hung_up = []

    async def record(_ctx, _args):
        hung_up.append(True)
        return "Call ended."

    end_call = _spec(name="end_call", tier="session_control", execute=record)
    talked = dataclasses.replace(_tctx(), spoke_since=lambda _t: True)
    assert await _make_handler(end_call, talked)({}, FakeRunContext()) == STILL_TALKING
    assert hung_up == []

    quiet = dataclasses.replace(_tctx(), spoke_since=lambda _t: False)
    assert await _make_handler(end_call, quiet)({}, FakeRunContext()) == "Call ended."
    assert hung_up == [True]


async def test_other_session_tools_run_while_they_talk():
    import dataclasses

    dtmf = _spec(name="send_dtmf", tier="session_control")
    talked = dataclasses.replace(_tctx(), spoke_since=lambda _t: True)
    assert await _make_handler(dtmf, talked)({}, FakeRunContext()) == "ok"


def test_speech_tracker_ignores_short_words():
    from hailhq.voicebot.tools import SpeechTracker

    speech = SpeechTracker()
    speech.on_state("speaking", 10.0)
    speech.on_state("listening", 10.4)  # "bye"
    assert not speech.talked_since(9.0, 11.0)

    speech.on_state("speaking", 12.0)
    assert not speech.talked_since(9.0, 12.5)  # still talking, not yet long
    assert speech.talked_since(9.0, 13.2)  # still talking, long enough
    speech.on_state("listening", 13.8)  # "What is that?"
    assert speech.talked_since(9.0, 14.0)
    assert not speech.talked_since(14.0, 14.5)  # it ended before `since`

    speech.on_state("speaking", 15.0)
    speech.on_state("listening", 15.3)  # a short "ok" after the long one
    assert speech.talked_since(9.0, 16.0)


async def test_connected_handover_asks_for_no_reply():
    """Once the two people talk, the muted agent must not write another
    reply: the tool returns None and the wrapper passes it to LiveKit,
    which then generates none."""

    async def connected(_ctx, _args):
        return None

    spec = _spec(name="transfer_call", tier="session_control", execute=connected)
    assert await _make_handler(spec, _tctx())({}, FakeRunContext()) is None


async def _seed_http_tool(session_factory, monkeypatch, **over):
    """An agent with one HTTP tool; returns (org_id, agent_id)."""
    key = generate_key()
    monkeypatch.setattr(settings, "hail_provider_secret_key", key)
    cipher = SecretCipher(key)
    org_id, agent_id = uuid.uuid4(), uuid.uuid4()
    async with session_factory() as s:
        s.add(
            Agent(
                id=agent_id,
                organization_id=org_id,
                name="Desk",
                system_prompt="Help.",
                http_tool_secret_encrypted=cipher.encrypt("hts_x"),
            )
        )
        await s.flush()
        s.add(
            AgentHttpTool(
                agent_id=agent_id,
                position=0,
                name="lookup_order",
                description="Find an order.",
                url="https://api.example.com/orders",
                parameters={"type": "object", "properties": {}},
                headers_encrypted=cipher.encrypt(json.dumps({"Authorization": "t"})),
                header_names=["Authorization"],
                timeout_seconds=5,
                wait_message="One moment.",
                **over,
            )
        )
        await s.commit()
    return org_id, agent_id


async def test_http_tools_load_for_the_calls_agent(db, session_factory, monkeypatch):
    org_id, agent_id = await _seed_http_tool(session_factory, monkeypatch)
    meta = {
        "organization_id": str(org_id),
        "agent_id": str(agent_id),
        # `tools: []` turns off built-ins only; the agent's own tools stay.
        "tools": [],
    }
    tools, api = await build_agent_tools(
        meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None
    )
    try:
        assert [t.info.name for t in tools] == [*EVERY_CALL, "lookup_order"]
        assert tools[-1].info.raw_schema["description"] == "Find an order."
    finally:
        if api is not None:
            await api.aclose()


async def test_http_tools_of_another_org_are_not_loaded(
    db, session_factory, monkeypatch
):
    _org, agent_id = await _seed_http_tool(session_factory, monkeypatch)
    meta = {
        "organization_id": str(uuid.uuid4()),
        "agent_id": str(agent_id),
        "tools": [],
    }
    tools, api = await build_agent_tools(
        meta, call_id=uuid.uuid4(), hangup=None, send_dtmf=None
    )
    try:
        assert [t.info.name for t in tools] == EVERY_CALL
    finally:
        if api is not None:
            await api.aclose()


async def test_http_tool_load_failure_keeps_built_in_tools(db, monkeypatch):
    async def broken(_session, _conv):
        raise RuntimeError("db down")

    # The loader catches the failure itself.
    monkeypatch.setattr(http_tool, "http_tool_specs", broken)
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "agent_id": str(uuid.uuid4())},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    try:
        assert "hail_settings_help" in [t.info.name for t in tools]
    finally:
        if api is not None:
            await api.aclose()


async def test_wait_message_is_said_without_waiting():
    """The sentence plays while the request runs: say() is not awaited."""
    import dataclasses

    order: list[str] = []

    class Session:
        def say(self, text):
            order.append(f"say:{text}")

    rc = FakeRunContext()
    rc.session = Session()

    async def execute(_ctx, _args):
        order.append("request")
        return "shipped"

    spec = dataclasses.replace(
        _spec(name="lookup_order", tier="customer_api", execute=execute),
        wait_message="One moment.",
    )
    assert await _make_handler(spec, _tctx())({}, rc) == "shipped"
    assert order == ["say:One moment.", "request"]
    assert rc.waited is False


async def test_failed_availability_check_closes_the_api_client(monkeypatch):
    """The caller gets no client when the build fails, so the build closes it."""
    closed = []

    class FakeClient:
        def __init__(self, *_args):
            pass

        async def aclose(self):
            closed.append(True)

    def db_down():
        raise RuntimeError("database down")

    monkeypatch.setattr(tools_mod, "AgentApiClient", FakeClient)
    monkeypatch.setattr(tools_mod, "session_scope", db_down)
    monkeypatch.setattr(settings, "hail_internal_secret", "secret", raising=False)
    with pytest.raises(RuntimeError):
        await build_agent_tools(
            {"organization_id": str(uuid.uuid4()), "tools": ["send_sms"]},
            call_id=uuid.uuid4(),
            hangup=None,
            send_dtmf=None,
        )
    assert closed == [True]
