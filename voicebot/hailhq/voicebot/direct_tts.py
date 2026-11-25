"""Connect direct vendor audio streams to the call worker."""

from contextlib import aclosing

import httpx
from hailhq.core.providers.tts.direct import SAMPLE_RATE, DirectSpeech
from livekit.agents import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    tts,
    utils,
)
from livekit.agents.types import APIConnectOptions, DEFAULT_API_CONNECT_OPTIONS


class DirectTTS(tts.TTS):
    def __init__(self, speech: DirectSpeech) -> None:
        # Vendors stream audio back for a complete phrase. The worker's
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
        return _DirectStream(tts=self, input_text=text, conn_options=conn_options)

    async def aclose(self) -> None:
        await self._client.aclose()


class _DirectStream(tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        model: DirectTTS = self._tts
        output_emitter.initialize(
            request_id=utils.shortuuid(),
            sample_rate=SAMPLE_RATE,
            num_channels=1,
            mime_type="audio/pcm",
        )
        try:
            async with aclosing(
                model.speech.pcm(model._client, self._input_text)
            ) as chunks:
                async for chunk in chunks:
                    output_emitter.push(chunk)
        except httpx.HTTPStatusError as exc:
            raise APIStatusError(
                f"{model.provider} synthesis failed",
                status_code=exc.response.status_code,
            ) from None
        except httpx.TimeoutException:
            raise APITimeoutError() from None
        except (httpx.HTTPError, ValueError):
            raise APIConnectionError(f"{model.provider} speech stream failed") from None
