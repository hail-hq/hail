"""Adapter interface for providers that return speech as mono PCM."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import ClassVar, Self

import httpx

SAMPLE_RATE = 24000


@dataclass(frozen=True)
class PCMAdapter(ABC):
    provider: ClassVar[str]
    model: ClassVar[str]
    default_voice: ClassVar[str]

    voice: str
    api_key: str | None = field(repr=False)

    @classmethod
    def house_key(cls) -> str | None:
        return None

    @classmethod
    def house_available(cls) -> bool:
        return bool(cls.house_key())

    @classmethod
    async def house_credentials_available(cls) -> bool:
        return cls.house_available()

    @classmethod
    def validate_options(cls, params: dict, *, has_workspace_key: bool) -> None:
        if params.get("model") not in (None, cls.model):
            raise ValueError(f"{cls.provider} speech uses {cls.model}.")
        if not has_workspace_key and not cls.house_available():
            raise ValueError(
                f"No workspace or Hail key is available for {cls.provider} speech."
            )

    @classmethod
    def from_config(
        cls, params: dict, api_key: str | None = None, voice: str | None = None
    ) -> Self:
        if api_key is not None and not api_key.strip():
            raise ValueError("Enter an API key.")
        cls.validate_options(params, has_workspace_key=api_key is not None)
        return cls(
            voice or params.get("voice_id") or cls.default_voice,
            api_key if api_key is not None else cls.house_key(),
        )

    @abstractmethod
    def pcm(self, client: httpx.AsyncClient, text: str) -> AsyncIterator[bytes]:
        """Yield signed 16-bit samples at the shared sample rate."""


async def stream_pcm(
    client: httpx.AsyncClient, url: str, key: str | None, body: dict
) -> AsyncIterator[bytes]:
    async with client.stream(
        "POST", url, headers={"Authorization": f"Bearer {key}"}, json=body
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            yield chunk
