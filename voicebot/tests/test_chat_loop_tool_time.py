"""HttpToolTime: the HTTP tools of one text or email reply share a time cap."""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace

from hailhq.core.agent_tools.spec import ToolSpec
from hailhq.voicebot import chat_loop
from hailhq.voicebot.chat_loop import NO_TOOL_TIME, HttpToolTime


async def _avail(_org, _session):
    return True


async def _execute(_ctx, _args):
    return "ok"


_HTTP = ToolSpec(
    name="lookup_order",
    description="d",
    parameters={"type": "object", "properties": {}},
    risk_tier="customer_api",
    is_available=_avail,
    execute=_execute,
)
_BUILT_IN = dataclasses.replace(_HTTP, name="thread_history", risk_tier="read_only")


async def test_http_tools_stop_once_a_full_timeout_no_longer_fits(monkeypatch):
    clock = iter([0.0, 10.0, 10.0, 20.0, 20.0, 30.0])
    # Replace the module's `time`, not time.monotonic itself: asyncio uses it.
    monkeypatch.setattr(
        chat_loop, "time", SimpleNamespace(monotonic=lambda: next(clock))
    )
    ran: list[str] = []

    async def run():
        ran.append("x")
        return "ok", False

    budget = HttpToolTime(30.0)
    for _ in range(3):
        assert await budget.run(_HTTP, run) == ("ok", False)
    # 30 s used: a fourth call could take 10 s more, so it does not start.
    assert await budget.run(_HTTP, run) == (NO_TOOL_TIME, True)
    assert len(ran) == 3
    # Built-in tools and unknown names do not count and always run.
    assert await budget.run(_BUILT_IN, run) == ("ok", False)
    assert await budget.run(None, run) == ("ok", False)
