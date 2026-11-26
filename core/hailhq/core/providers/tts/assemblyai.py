"""AssemblyAI text-to-speech adapter."""

from collections.abc import AsyncIterator

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.tts.base import SAMPLE_RATE, PCMAdapter, stream_pcm

TTS_URL = "https://tts.us.assemblyai.com/v1/openai/audio/speech"


class AssemblyAISpeech(PCMAdapter):
    provider = "assemblyai"
    model = "assemblyai-tts-1"
    default_voice = "aai-audrey"

    @classmethod
    def house_key(cls) -> str:
        return settings.assemblyai_tts_api_key

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
