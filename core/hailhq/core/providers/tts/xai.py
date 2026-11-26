"""xAI text-to-speech adapter."""

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING

import httpx
from hailhq.core.config import settings
from hailhq.core.providers.tts.base import SAMPLE_RATE, PCMAdapter, stream_pcm

if TYPE_CHECKING:
    from livekit.agents.tts import TTS

TTS_URL = "https://api.x.ai/v1/tts"


class XaiSpeech(PCMAdapter):
    provider = "xai"
    model = "tts"
    default_voice = "ara"

    @classmethod
    def house_key(cls) -> str:
        return settings.xai_api_key

    def livekit_tts(self) -> "TTS":
        from hailhq.core.providers.tts.livekit_deadline import XaiTTS
        from livekit.agents import tokenize

        return XaiTTS(
            api_key=self.api_key,
            voice=self.voice,
            language="en",
            tokenizer=tokenize.basic.WordTokenizer(
                ignore_punctuation=False, retain_format=True
            ),
        )

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
