"""Use direct Gemini synthesis for credentials the LiveKit plugin cannot accept."""

import asyncio
from contextlib import aclosing

import httpx
from hailhq.core.providers.tts.base import SAMPLE_RATE
from hailhq.core.providers.tts.gemini import GeminiSpeech
from livekit.agents import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    tts,
    utils,
)
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions

_FRAME_MS = 20
_INITIAL_PCM_BYTES = SAMPLE_RATE * _FRAME_MS // 1000 * 2


class GeminiDirectTTS(tts.TTS):
    def __init__(self, speech: GeminiSpeech) -> None:
        # Gemini streams audio back for a complete phrase. The worker's
        # StreamAdapter feeds this adapter one phrase at a time.
        super().__init__(
            capabilities=tts.TTSCapabilities(streaming=False),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
        )
        self.speech = speech
        self._client = httpx.AsyncClient(timeout=12.0)

    @property
    def model(self) -> str:
        return self.speech.model

    @property
    def provider(self) -> str:
        return self.speech.provider

    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS,
    ) -> tts.ChunkedStream:
        return _PCMStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class _PCMStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        model: GeminiDirectTTS = self._tts
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            mime_type="audio/pcm",
            frame_size_ms=_FRAME_MS,
        )
        try:
            async with aclosing(
                model.speech.pcm(model._client, self._input_text)
            ) as chunks:
                async with asyncio.timeout(self._conn_options.timeout):
                    first = bytearray()
                    while len(first) < _INITIAL_PCM_BYTES:
                        chunk = await anext(chunks, None)
                        if chunk is None:
                            break
                        first.extend(chunk)
                if len(first) < 2:
                    raise APIConnectionError(
                        f"{model.provider} returned no speech audio"
                    )
                output_emitter.push(bytes(first))
                async for chunk in chunks:
                    output_emitter.push(chunk)
        except httpx.HTTPStatusError as exc:
            raise APIStatusError(
                f"{model.provider} synthesis failed",
                status_code=exc.response.status_code,
            ) from None
        except (httpx.TimeoutException, TimeoutError):
            raise APITimeoutError(
                retryable=output_emitter.pushed_duration() == 0
            ) from None
        except (httpx.HTTPError, ValueError):
            raise APIConnectionError(f"{model.provider} speech stream failed") from None
