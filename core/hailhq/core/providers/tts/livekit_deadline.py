"""Keep native speech plugins within the call's audio deadline."""

import asyncio
from contextlib import asynccontextmanager
from typing import cast

from livekit.agents import APIConnectionError, APIError, APITimeoutError, tokenize, tts
from livekit.agents.types import DEFAULT_API_CONNECT_OPTIONS, APIConnectOptions
from livekit.plugins.google.beta import gemini_tts
from livekit.plugins.xai import tts as xai_tts


class _ProgressEmitter:
    def __init__(
        self,
        emitter: tts.AudioEmitter,
        deadline: asyncio.Timeout,
        interval: float,
        sample_rate: int,
    ):
        self._emitter = emitter
        self._deadline = deadline
        self._interval = interval
        self._now = asyncio.get_running_loop().time
        # Count 20 ms of mono PCM, the same progress unit as direct synthesis.
        self._frame_bytes = sample_rate * 2 // 50
        self._buffered = 0

    def __getattr__(self, name: str):
        return getattr(self._emitter, name)

    def start(self) -> None:
        self._deadline.reschedule(self._now() + self._interval)

    def pause(self) -> None:
        if not self._deadline.expired():
            self._deadline.reschedule(None)

    def push(self, data: bytes) -> None:
        if self._deadline.expired():
            return
        self._emitter.push(data)
        self._buffered += len(data)
        if self._buffered >= self._frame_bytes:
            self._buffered %= self._frame_bytes
            self.start()


@asynccontextmanager
async def _audio_deadline(emitter: tts.AudioEmitter, timeout: float, sample_rate: int):
    async with asyncio.timeout(timeout) as deadline:
        yield _ProgressEmitter(emitter, deadline, timeout, sample_rate)


class _GeminiStream(gemini_tts.ChunkedStream):
    async def _run(self, output_emitter: tts.AudioEmitter) -> None:
        try:
            async with _audio_deadline(
                output_emitter, self._conn_options.timeout, self._tts.sample_rate
            ) as emitter:
                await super()._run(cast(tts.AudioEmitter, emitter))
        except TimeoutError:
            raise APITimeoutError(
                retryable=output_emitter.pushed_duration() == 0
            ) from None
        except APIError as exc:
            if exc.retryable and output_emitter.pushed_duration() > 0:
                raise APIConnectionError(
                    "Gemini speech failed after audio was delivered.", retryable=False
                ) from exc
            raise


class GeminiTTS(gemini_tts.TTS):
    def synthesize(
        self,
        text: str,
        *,
        conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> tts.ChunkedStream:
        return _GeminiStream(tts=self, input_text=text, conn_options=conn_options)


class _XaiStream(xai_tts.SynthesizeStream):
    async def _run_ws(
        self, input_stream: tokenize.WordStream, output_emitter: tts.AudioEmitter
    ) -> None:
        async with _audio_deadline(
            output_emitter,
            self._conn_options.timeout,
            self._tts.sample_rate,
        ) as emitter:

            async def outgoing_words():
                # The SDK starts reading words only after it acquires a socket.
                emitter.pause()
                words = aiter(input_stream)
                first = await anext(words, None)
                # Begin when the plugin can send its first word or text.done.
                emitter.start()
                if first is not None:
                    yield first
                    async for word in words:
                        yield word

            await super()._run_ws(
                cast(tokenize.WordStream, outgoing_words()),
                cast(tts.AudioEmitter, emitter),
            )


class XaiTTS(xai_tts.TTS):
    def stream(
        self, *, conn_options: APIConnectOptions = DEFAULT_API_CONNECT_OPTIONS
    ) -> xai_tts.SynthesizeStream:
        stream = _XaiStream(tts=self, conn_options=conn_options)
        self._streams.add(stream)
        return stream
