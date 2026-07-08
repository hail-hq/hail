"""Per-org BYO provider config: layer vocabulary, param schemas, cipher.

The single source of truth for what each pipeline layer accepts. Routes
(``api/routes/internal/provider_config``) validate writes through
``PARAMS_BY_LAYER``; the voicebot resolves reads through
``load_org_provider_configs``. Model names are free text everywhere — the
live key validation (``provider_validation``) is what proves a key+model
pair works, never a hardcoded list.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.secret_cipher import SecretCipher

__all__ = [
    "LAYERS",
    "PROVIDERS_BY_LAYER",
    "LLMParams",
    "TTSParams",
    "STTParams",
    "PARAMS_BY_LAYER",
    "provider_cipher",
    "last4",
    "load_org_provider_configs",
]

LAYERS: tuple[str, ...] = ("llm", "tts", "stt")

PROVIDERS_BY_LAYER: dict[str, tuple[str, ...]] = {
    "llm": ("openai-compatible", "anthropic", "google"),
    "tts": ("cartesia", "elevenlabs"),
    "stt": ("deepgram",),
}


class LLMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai-compatible", "anthropic", "google"]
    base_url: str | None = None
    model: str

    @model_validator(mode="after")
    def _base_url_matches_provider(self) -> "LLMParams":
        if self.provider == "openai-compatible" and not self.base_url:
            raise ValueError("base_url is required for openai-compatible")
        if self.provider != "openai-compatible" and self.base_url:
            raise ValueError("base_url only applies to openai-compatible")
        return self


class TTSParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["cartesia", "elevenlabs"]
    voice_id: str | None = None
    model: str | None = None


class STTParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["deepgram"]
    model: str | None = None


PARAMS_BY_LAYER: dict[str, type[BaseModel]] = {
    "llm": LLMParams,
    "tts": TTSParams,
    "stt": STTParams,
}


def provider_cipher() -> SecretCipher:
    """Cipher for provider keys. Raises SecretKeyMissing when unset."""
    return SecretCipher(settings.hail_provider_secret_key)


def last4(key: str) -> str:
    return key[-4:]


async def load_org_provider_configs(
    session: AsyncSession, organization_id: UUID
) -> dict[str, OrgProviderConfig]:
    rows = (
        (
            await session.execute(
                select(OrgProviderConfig).where(
                    OrgProviderConfig.organization_id == organization_id
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.layer: row for row in rows}
