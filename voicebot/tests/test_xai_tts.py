import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from hailhq.core.providers.tts.xai import XaiSpeech
from livekit.agents import APIConnectOptions, APITimeoutError


async def test_xai_times_out_after_sending_text_to_a_silent_socket(monkeypatch):
    async def stall():
        await asyncio.Event().wait()

    socket = Mock(
        send_str=AsyncMock(), receive=AsyncMock(side_effect=stall), close=AsyncMock()
    )
    session = SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
    renderer = XaiSpeech.from_config({}, "fixture-key").livekit_tts()
    monkeypatch.setattr(renderer, "_ensure_session", lambda: session)
    try:
        options = APIConnectOptions(timeout=0.05, max_retry=0)
        async with renderer.stream(conn_options=options) as stream:
            stream.push_text("Your order ")
            stream.push_text("is ready.")
            stream.end_input()
            async with asyncio.timeout(1):
                with pytest.raises(APITimeoutError):
                    await anext(stream)
    finally:
        await renderer.aclose()
    messages = [json.loads(call.args[0]) for call in socket.send_str.await_args_list]
    assert (
        "".join(message.get("delta", "") for message in messages)
        == "Your order is ready."
    )
    assert messages[-1] == {"type": "text.done"}
    socket.close.assert_awaited_once()
