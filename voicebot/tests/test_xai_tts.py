import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from aiohttp import WSMessage, WSMsgType
from hailhq.voicebot.pipeline import ResolvedLayer, build_tts
from livekit.agents import APIConnectOptions
from livekit.plugins import xai


async def test_xai_sends_intact_text_and_plays_provider_audio(monkeypatch):
    input_done = asyncio.Event()
    pcm = b"\x01\x00" * 480

    async def send(message):
        if json.loads(message)["type"] == "text.done":
            input_done.set()

    async def receive():
        await input_done.wait()
        message = next(provider_messages)
        return WSMessage(WSMsgType.TEXT, json.dumps(message), "")

    provider_messages = iter(
        [
            {"type": "audio.delta", "delta": base64.b64encode(pcm).decode()},
            {"type": "audio.done"},
        ]
    )
    socket = Mock(
        send_str=AsyncMock(side_effect=send),
        receive=AsyncMock(side_effect=receive),
        close=AsyncMock(),
    )
    session = SimpleNamespace(ws_connect=AsyncMock(return_value=socket))
    renderer = build_tts(ResolvedLayer("xai", "fixture-key", {}, False), language="en")
    assert type(renderer) is xai.TTS
    monkeypatch.setattr(renderer, "_ensure_session", lambda: session)
    try:
        options = APIConnectOptions(max_retry=0)
        async with renderer.stream(conn_options=options) as stream:
            async with asyncio.timeout(2):
                stream.push_text("Yo")
                stream.push_text("ur order is ready.")
                stream.end_input()
                assert (
                    sum(
                        [
                            sample != 0
                            async for chunk in stream
                            for sample in chunk.frame.data
                        ]
                    )
                    == 480
                )
    finally:
        await renderer.aclose()
    messages = [json.loads(call.args[0]) for call in socket.send_str.await_args_list]
    assert (
        "".join(message.get("delta", "") for message in messages)
        == "Your order is ready."
    )
