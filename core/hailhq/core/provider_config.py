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
from urllib.parse import urlsplit
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.models import OrgProviderConfig
from hailhq.core.secret_cipher import SecretCipher
from pydantic import BaseModel, ConfigDict, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

__all__ = [
    "LAYERS",
    "PARAMS_BY_LAYER",
    "LLMParams",
    "STTParams",
    "TTSParams",
    "last4",
    "load_org_provider_configs",
    "provider_cipher",
]

LAYERS: tuple[str, ...] = ("llm", "tts", "stt")


class LLMParams(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: Literal["openai-compatible", "anthropic", "google"]
    base_url: str | None = None
    model: str

    @field_validator("base_url")
    @classmethod
    def _base_url_is_public_https(cls, v: str | None) -> str | None:
        """Cheap syntactic check only — no name resolution.

        Mirrors ``LLMConfig``'s check in ``core/hailhq/core/schemas.py``: it
        catches an obviously-unsafe value immediately on save. The full
        resolving SSRF check (``assert_public_https_url``) is deliberately
        NOT run here — this validator runs synchronously on whichever event
        loop calls it, and that check does blocking DNS. The resolving check
        still runs, off the event loop, in ``resolve_org_configs`` before
        this config is ever used to build an LLM.
        """
        if v is None:
            return v
        parts = urlsplit(v)
        if parts.scheme != "https":
            raise ValueError(f"base_url must use https, got '{parts.scheme or v}'")
        if not parts.hostname:
            raise ValueError("base_url has no host")
        return v

    @model_validator(mode="after")
    def _base_url_matches_provider(self) -> LLMParams:
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

    provider: Literal["deepgram", "speechmatics"]
    model: str | None = None
    # Speechmatics transcription tier. None -> enhanced. Ignored on deepgram
    # rows (accepted-but-unused would violate "no hidden behavior", so the
    # validator below rejects it there).
    operating_point: Literal["enhanced", "standard"] | None = None

    @model_validator(mode="after")
    def _operating_point_is_speechmatics_only(self) -> STTParams:
        if self.provider != "speechmatics" and self.operating_point is not None:
            raise ValueError("operating_point only applies to speechmatics")
        return self


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
                    OrgProviderConfig.organization_id == organization_id,
                    OrgProviderConfig.is_active,
                )
            )
        )
        .scalars()
        .all()
    )
    return {row.layer: row for row in rows}
