"""xAI text-to-speech adapter."""

from collections.abc import AsyncIterator

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.tts.base import SAMPLE_RATE, PCMAdapter, stream_pcm

TTS_URL = "https://api.x.ai/v1/tts"


class XaiSpeech(PCMAdapter):
    provider = "xai"
    model = "tts"
    default_voice = "ara"

    @classmethod
    def house_key(cls) -> str:
        return settings.xai_tts_api_key

    def pcm(self, client: httpx.AsyncClient, text: str) -> AsyncIterator[bytes]:
        return stream_pcm(
            client,
            TTS_URL,
            self.api_key,
            {
                "text": text,
                "voice_id": self.voice,
                "language": "en",
                "output_format": {"codec": "pcm", "sample_rate": SAMPLE_RATE},
            },
        )
