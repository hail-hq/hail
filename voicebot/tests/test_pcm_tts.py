"""The app's audio deadline applies to the first PCM frame."""

import asyncio

import pytest
from hailhq.core.providers.tts.xai import XaiSpeech
from hailhq.voicebot.pcm_tts import PCMTTS
from livekit.agents import APIConnectOptions, APITimeoutError


async def test_pcm_stream_respects_first_audio_timeout(monkeypatch):
    async def stalled_pcm(self, client, text):
        yield b""
        await asyncio.Event().wait()

    monkeypatch.setattr(XaiSpeech, "pcm", stalled_pcm)
    renderer = PCMTTS(XaiSpeech.from_config({}, "fixture-key"))
    try:
        options = APIConnectOptions(timeout=0.02, max_retry=0)
        async with renderer.synthesize("Hello.", conn_options=options) as stream:
            with pytest.raises(APITimeoutError):
                await asyncio.wait_for(anext(stream), timeout=1)
    finally:
        await renderer.aclose()
