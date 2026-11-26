"""AssemblyAI text-to-speech adapter."""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.tts.base import SAMPLE_RATE, PCMAdapter, stream_pcm

if TYPE_CHECKING:
    from livekit.agents.tts import TTS

TTS_URL = "https://tts.us.assemblyai.com/v1/openai/audio/speech"


class AssemblyAISpeech(PCMAdapter):
    provider = "assemblyai"
    model = "assemblyai-tts-1"
    default_voice = "aai-audrey"

    @classmethod
    def house_key(cls) -> str:
        return settings.assemblyai_api_key

    def livekit_tts(self) -> "TTS":
        from livekit.plugins.assemblyai import TTS

        return TTS(
            api_key=self.api_key, model=self.model, voice=self.voice, language="en"
        )

    def pcm(self, client: httpx.AsyncClient, text: str) -> AsyncIterator[bytes]:
        return stream_pcm(
            client,
            TTS_URL,
            self.api_key,
            {
                "input": text,
                "voice": self.voice,
                "model": self.model,
                "language": "en",
                "response_format": "pcm",
                "sample_rate": SAMPLE_RATE,
            },
        )
