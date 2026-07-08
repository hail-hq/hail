"""Voice-pipeline assembly for the Hail voicebot worker.

Centralizes the ``AgentSession`` construction so :mod:`hailhq.voicebot.agent`
stays focused on lifecycle (connect, parse metadata, attach event handlers,
clean up). Three LLM modes are supported:

* **Mode A — Hail fallback chain** (``llm`` is ``None`` and no org BYO llm
  config): assemble a :class:`livekit.agents.llm.FallbackAdapter` over
  OpenAI → Google → Anthropic fast-tier models so a single provider outage
  doesn't take the call down.
* **Mode B — caller-provided endpoint** (dispatch metadata's ``llm`` is a
  dict with ``base_url``/``api_key``/``model``, possibly decrypted from
  ``api_key_enc``): a single :class:`livekit.plugins.openai.LLM` pointed at
  the OpenAI-compatible endpoint the caller supplied. No fallback — the
  caller chose this brain explicitly. Takes precedence over mode C.
* **Mode C — per-org BYO provider config** (``resolve_org_configs`` finds a
  row for the org): the org's own key/provider is used for the layer, with
  Hail's own key used as a fallback only if the org opted in
  (``fallback_enabled``). Absent a per-call override, this is the org's
  standing choice.

Precedence per layer: per-call (mode B, TTS/STT have no per-call override
today) > org BYO (mode C) > house default (mode A). A BYO layer that cannot
be built (no key, unknown provider, unsafe URL) and has fallback disabled
raises :class:`ProviderKeyError` — deliberately fail-fast, since silently
substituting Hail's keys would defeat the point of BYO unless the org opted
into the fallback.

API surface verified 2026-04-28 against:

* ``livekit-agents/livekit/agents/voice/agent_session.py`` (AgentSession init).
* ``livekit-agents/livekit/agents/llm/fallback_adapter.py`` (FallbackAdapter).
* ``livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/llm.py``
  (the openai plugin's LLM accepts ``base_url``/``api_key``/``model``).

Plugin ``api_key`` keyword support (2026-07-08, task 6 Step 0): confirmed
present on ``cartesia.TTS``, ``elevenlabs.TTS``, ``deepgram.STT``,
``anthropic.LLM``, ``google.LLM``, ``openai.LLM`` in the installed version.
``livekit.agents.llm.FallbackAdapter`` stores its wrapped instances on the
private ``_llm_instances`` attribute (no public accessor), so the BYO+fallback
branch below constructs the three house LLMs inline rather than reaching into
a private attribute of ``_house_llm()``.
"""

from __future__ import annotations

import dataclasses
from typing import Any
from uuid import UUID

from livekit.agents import AgentSession
from livekit.agents import llm as agents_llm
from livekit.agents import stt as agents_stt
from livekit.agents import tts as agents_tts
from livekit.agents import vad as agents_vad
from livekit.plugins import (
    anthropic as anthropic_plugin,
)
from livekit.plugins import (
    cartesia as cartesia_plugin,
)
from livekit.plugins import (
    deepgram as deepgram_plugin,
)
from livekit.plugins import (
    elevenlabs as elevenlabs_plugin,
)
from livekit.plugins import (
    google as google_plugin,
)
from livekit.plugins import (
    openai as openai_plugin,
)

from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.provider_config import load_org_provider_configs, provider_cipher
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url

__all__ = [
    "ProviderKeyError",
    "ResolvedLayer",
    "resolve_org_configs",
    "decrypt_llm_metadata",
    "build_llm",
    "build_tts",
    "build_stt",
    "build_session",
]


class ProviderKeyError(RuntimeError):
    """A BYO layer could not be built and fallback is disabled.

    agent.py maps this to end_reason='provider_key_error' (fail-fast by
    design - the org chose BYO deliberately; silently substituting Hail's
    keys would defeat the point unless they opted in).
    """


@dataclasses.dataclass(frozen=True)
class ResolvedLayer:
    provider: str
    api_key: str | None
    params: dict
    fallback_enabled: bool


async def resolve_org_configs(
    organization_id: UUID | None,
) -> dict[str, ResolvedLayer]:
    """Load + decrypt the org's BYO rows. {} when org id absent or no rows."""
    if organization_id is None:
        return {}
    async with session_scope() as session:
        rows = await load_org_provider_configs(session, organization_id)
    resolved: dict[str, ResolvedLayer] = {}
    for layer, row in rows.items():
        api_key: str | None = None
        if row.encrypted_api_key is not None:
            api_key = provider_cipher().decrypt(row.encrypted_api_key)
        resolved[layer] = ResolvedLayer(
            provider=row.provider,
            api_key=api_key,
            params=dict(row.params),
            fallback_enabled=row.fallback_enabled,
        )
    return resolved


def decrypt_llm_metadata(llm_cfg: dict[str, Any] | None) -> dict[str, Any] | None:
    """Per-call llm from dispatch metadata -> plaintext-key dict.

    Accepts the encrypted shape (api_key_enc, from an API with
    HAIL_PROVIDER_SECRET_KEY set) and the legacy plaintext shape.
    """
    if llm_cfg is None:
        return None
    if "api_key_enc" in llm_cfg:
        cfg = dict(llm_cfg)
        cfg["api_key"] = provider_cipher().decrypt(cfg.pop("api_key_enc"))
        return cfg
    return llm_cfg


def _house_llm() -> agents_llm.LLM:
    return agents_llm.FallbackAdapter(
        llm=[
            openai_plugin.LLM(model=settings.openai_model),
            google_plugin.LLM(model=settings.google_model),
            anthropic_plugin.LLM(model=settings.anthropic_model),
        ],
        attempt_timeout=10.0,
        max_retry_per_llm=1,
        retry_interval=5.0,
    )


def _org_llm(org: ResolvedLayer) -> agents_llm.LLM:
    if org.api_key is None:
        raise ProviderKeyError("org llm config has no stored api key")
    model = org.params.get("model", "")
    if org.provider == "openai-compatible":
        # SSRF guard at call time too: a stored base_url could have been
        # written before the guard existed, or DNS could have re-pointed.
        try:
            base_url = assert_public_https_url(org.params["base_url"])
        except UnsafeUrlError as exc:
            raise ProviderKeyError(f"org llm base_url is not permitted: {exc}") from exc
        return openai_plugin.LLM(base_url=base_url, api_key=org.api_key, model=model)
    if org.provider == "anthropic":
        return anthropic_plugin.LLM(api_key=org.api_key, model=model)
    if org.provider == "google":
        return google_plugin.LLM(api_key=org.api_key, model=model)
    raise ProviderKeyError(f"unknown org llm provider '{org.provider}'")


def build_llm(
    llm_cfg: dict[str, Any] | None, org: ResolvedLayer | None = None
) -> agents_llm.LLM:
    """Precedence: per-call llm (mode B) > org BYO (mode C) > house chain (mode A)."""
    if llm_cfg is not None:
        return openai_plugin.LLM(
            base_url=llm_cfg["base_url"],
            api_key=llm_cfg["api_key"],
            model=llm_cfg["model"],
        )
    if org is not None:
        byo = _org_llm(org)
        if org.fallback_enabled:
            return agents_llm.FallbackAdapter(
                llm=[
                    byo,
                    openai_plugin.LLM(model=settings.openai_model),
                    google_plugin.LLM(model=settings.google_model),
                    anthropic_plugin.LLM(model=settings.anthropic_model),
                ],
                attempt_timeout=10.0,
                max_retry_per_llm=1,
                retry_interval=5.0,
            )
        return byo
    return _house_llm()


def _house_tts(voice_id_override: str | None) -> list[agents_tts.TTS]:
    instances: list[agents_tts.TTS] = []
    if settings.cartesia_api_key:
        instances.append(
            cartesia_plugin.TTS(
                model=settings.cartesia_model,
                voice=voice_id_override or settings.cartesia_voice_id,
            )
        )
    if settings.eleven_api_key:
        instances.append(
            elevenlabs_plugin.TTS(
                voice_id=voice_id_override or settings.elevenlabs_voice_id,
                model=settings.elevenlabs_model,
            )
        )
    return instances


def _org_tts(org: ResolvedLayer, voice_id_override: str | None) -> agents_tts.TTS:
    voice = voice_id_override or org.params.get("voice_id")
    if org.provider == "cartesia":
        kwargs: dict[str, Any] = {
            "model": org.params.get("model") or settings.cartesia_model
        }
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.cartesia_api_key:
            raise ProviderKeyError("no org or house cartesia key available")
        if voice:
            kwargs["voice"] = voice
        return cartesia_plugin.TTS(**kwargs)
    if org.provider == "elevenlabs":
        kwargs = {"model": org.params.get("model") or settings.elevenlabs_model}
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.eleven_api_key:
            raise ProviderKeyError("no org or house elevenlabs key available")
        if voice:
            kwargs["voice_id"] = voice
        return elevenlabs_plugin.TTS(**kwargs)
    raise ProviderKeyError(f"unknown org tts provider '{org.provider}'")


def build_tts(
    org: ResolvedLayer | None = None, voice_id_override: str | None = None
) -> agents_tts.TTS:
    """Construct the TTS for one call.

    ``org`` present (mode C): the org's provider is used, with Hail's house
    TTS instances appended as a fallback only if ``org.fallback_enabled``.
    ``org`` absent (mode A): Cartesia primary, ElevenLabs fallback — a
    provider is included only when its API key is configured, so a
    single-key self-host still works (tenet 4). With both keys set the two
    are wrapped in a ``FallbackAdapter`` with Cartesia first. With one key
    set that provider is used directly with no adapter.
    """
    if org is not None:
        byo = _org_tts(org, voice_id_override)
        if org.fallback_enabled:
            house = _house_tts(voice_id_override)
            if house:
                return agents_tts.FallbackAdapter([byo, *house])
        return byo
    instances = _house_tts(voice_id_override)
    if not instances:
        raise RuntimeError(
            "No TTS provider configured: set CARTESIA_API_KEY or ELEVEN_API_KEY."
        )
    if len(instances) == 1:
        return instances[0]
    return agents_tts.FallbackAdapter(instances)


def build_stt(org: ResolvedLayer | None = None) -> agents_stt.STT:
    """Construct the STT for one call.

    ``org`` present (mode C, deepgram only today): the org's key, with
    Hail's house Deepgram key appended as a fallback only if
    ``org.fallback_enabled`` and a house key is configured. ``org`` absent
    (mode A): Hail's house Deepgram key.
    """
    if org is not None and org.provider == "deepgram":
        kwargs: dict[str, Any] = {
            "model": org.params.get("model") or settings.deepgram_model
        }
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        byo = deepgram_plugin.STT(**kwargs)
        if org.fallback_enabled and settings.deepgram_api_key:
            return agents_stt.FallbackAdapter(
                [byo, deepgram_plugin.STT(model=settings.deepgram_model)]
            )
        return byo
    return deepgram_plugin.STT(model=settings.deepgram_model)


def build_session(
    llm_cfg: dict[str, Any] | None,
    vad: agents_vad.VAD,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    voice_id_override: str | None = None,
) -> AgentSession:
    """Build the :class:`AgentSession` for one job.

    ``vad`` is the per-process Silero instance loaded once in
    :func:`hailhq.voicebot.agent.prewarm`. ``org_cfgs`` is the org's resolved
    BYO layers (from :func:`resolve_org_configs`); ``voice_id_override`` is
    the per-call ``voice_config.voice_id`` from dispatch metadata, which
    beats any org-level default voice.
    """
    org_cfgs = org_cfgs or {}
    return AgentSession(
        vad=vad,
        stt=build_stt(org_cfgs.get("stt")),
        tts=build_tts(org_cfgs.get("tts"), voice_id_override),
        llm=build_llm(llm_cfg, org_cfgs.get("llm")),
    )
