import pytest
from types import SimpleNamespace
from hailhq.voicebot.browser import browser_instructions, deadline_seconds


def test_browser_runtime_is_private_and_does_not_promise_real_sends():
    result = browser_instructions("Collect a callback request.")
    assert "Collect a callback request." in result
    assert "browser" in result
    assert "never sends" in result
    assert "telephone call" not in result


def test_server_deadline_uses_persisted_budget_not_dispatch_input():
    assert deadline_seconds(SimpleNamespace(max_duration_seconds=60)) == 60
    assert deadline_seconds(SimpleNamespace(max_duration_seconds=600)) == 600
    assert deadline_seconds(SimpleNamespace(max_duration_seconds=99999)) == 600


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_fails", [False, True])
@pytest.mark.parametrize("shutdown_during_disconnect", [False, True])
async def test_hard_deadline_closes_room_and_finalizes_trial_without_client_timer(
    async_session, monkeypatch, delete_fails, shutdown_during_disconnect
):
    import asyncio
    import uuid
    from unittest.mock import AsyncMock
    from hailhq.core.models import Call
    from hailhq.voicebot import browser

    call = Call(
        organization_id=uuid.uuid4(),
        transport="browser",
        direction="inbound",
        provider="browser",
        voice_config={},
        max_duration_seconds=1,
        metadata_={"browser_trial": True},
        status="queued",
    )
    async_session.add(call)
    await async_session.commit()
    closed = asyncio.Event()

    class Session:
        history = SimpleNamespace(to_dict=lambda: {"items": []})

        def on(self, event):
            return lambda callback: callback

        async def start(self, **kwargs):
            pass

        async def aclose(self):
            pass

        def generate_reply(self, **kwargs):
            pass

    class Room:
        disconnect = AsyncMock()
        local_participant = SimpleNamespace(
            set_attributes=AsyncMock(), publish_data=AsyncMock()
        )

        def on(self, event):
            return lambda callback: callback

    class Context:
        room = Room()
        proc = SimpleNamespace(userdata={"vad": None})

        def add_shutdown_callback(self, fn):
            self.callback = fn

        async def connect(self):
            pass

        async def wait_for_participant(self):
            return SimpleNamespace(identity="browser-user")

        async def delete_room(self):
            if delete_fails:
                raise TimeoutError("room API unavailable")
            closed.set()

        def shutdown(self, **kwargs):
            closed.set()

    monkeypatch.setattr(browser, "resolve_org_configs", AsyncMock(return_value={}))
    monkeypatch.setattr(browser, "build_session", lambda *a, **kw: Session())
    ctx = Context()
    if shutdown_during_disconnect:

        async def disconnected():
            asyncio.create_task(ctx.callback())

        ctx.room.disconnect.side_effect = disconnected
    await browser.run_browser_session(ctx, call.id)
    await asyncio.wait_for(closed.wait(), timeout=3)
    await asyncio.sleep(0.1)
    await async_session.refresh(call)
    ctx.room.disconnect.assert_awaited()
    assert call.status == "completed"
    assert call.metadata_["browser_used_ms"] == 1000
    await ctx.callback()


def test_transcript_does_not_expose_system_instructions_or_tool_results():
    from hailhq.voicebot.browser import public_transcript

    result = public_transcript(
        {
            "items": [
                {"role": "system", "content": ["private runtime"]},
                {"role": "user", "content": ["Hello", {"audio": "bytes"}]},
                {"role": "tool", "content": ["internal"]},
            ]
        }
    )
    assert result == {"items": [{"role": "user", "content": ["Hello"]}]}
