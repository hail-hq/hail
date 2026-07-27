"""Registry → LiveKit adaptation: opt-out, degradation, error isolation."""

from __future__ import annotations

import uuid

from hailhq.voicebot.tools import (
    SPOKEN_TOOL_FAILURE,
    _make_handler,
    build_agent_tools,
)


class FakeRunContext:
    def __init__(self):
        self.waited = False

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
