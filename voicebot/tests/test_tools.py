"""Registry → LiveKit adaptation: opt-out, degradation, error isolation."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

from hailhq.voicebot.tools import (
    SPOKEN_TOOL_FAILURE,
    _make_handler,
    build_agent_tools,
)


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


async def test_empty_opt_out_disables_all_tools():
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "tools": []},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    assert tools == []
    assert api is None


async def test_missing_organization_id_disables_tools():
    tools, api = await build_agent_tools(
        {}, call_id=uuid.uuid4(), hangup=None, send_dtmf=None
    )
    assert tools == []
    assert api is None


async def test_malformed_organization_id_disables_tools():
    tools, api = await build_agent_tools(
        {"organization_id": "not-a-uuid"},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    assert tools == []
    assert api is None


async def test_malformed_tools_field_disables_tools():
    tools, api = await build_agent_tools(
        {"organization_id": str(uuid.uuid4()), "tools": "send_sms"},
        call_id=uuid.uuid4(),
        hangup=None,
        send_dtmf=None,
    )
    assert tools == []
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
    import hailhq.voicebot.tools as tools_mod
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
    monkeypatch.setattr(tools_mod, "all_tools", lambda: specs)

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
        assert "transfer_call" in names
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
        assert "transfer_call" not in [t.info.name for t in tools]
    finally:
        if api is not None:
            await api.aclose()


async def test_uninterruptible_tool_disallows_interruptions():
    import dataclasses

    spec = dataclasses.replace(_spec(tier="session_control"), uninterruptible=True)
    rc = FakeRunContext()
    await _make_handler(spec, _tctx())({}, rc)
    assert rc.interruptions_allowed is False


async def test_end_call_keeps_the_call_when_they_talk_over_the_goodbye():
    import dataclasses

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
