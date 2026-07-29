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

Speechmatics plugin surface (2026-07-29, multi-language task): verified
``speechmatics.STT`` kwargs (``language``, ``operating_point``,
``api_key``, ``turn_detection_mode``, ``end_of_utterance_silence_trigger``)
and ``TurnDetectionMode`` members against the installed
livekit-plugins-speechmatics (1.6.6). All names match as expected; no
deviations. ``TurnDetectionMode.ADAPTIVE`` constructs without error —
``livekit-plugins-speechmatics`` pulls in ``speechmatics-voice[smart]`` as a
hard (non-optional) dependency, so the smart-turn extra (onnxruntime,
transformers) is already installed by a plain
``uv sync --all-packages --all-extras``. No fallback to
``TurnDetectionMode.FIXED`` is needed.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.languages import (
    SUPPORTED_LANGUAGES,
    resolve_stt_provider,
    turn_mode_for,
)
from hailhq.core.provider_config import load_org_provider_configs, provider_cipher
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
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
from livekit.plugins import (
    speechmatics as speechmatics_plugin,
)
from livekit.plugins.turn_detector.multilingual import MultilingualModel

logger = logging.getLogger("hailhq.voicebot")

__all__ = [
    "ProviderKeyError",
    "ResolvedLayer",
    "build_llm",
    "build_session",
    "build_stt",
    "build_tts",
    "decrypt_llm_metadata",
    "resolve_org_configs",
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
    *,
    skip_llm: bool = False,
) -> dict[str, ResolvedLayer]:
    """Load + decrypt the org's BYO rows. {} when org id absent or no rows.

    For the ``llm`` layer on ``openai-compatible``, also re-runs the
    resolving SSRF guard here (off the event loop via ``asyncio.to_thread``)
    rather than at LLM-build time in ``_org_llm``: a stored base_url could
    have been written before the guard existed, or DNS could have
    re-pointed since. Raising ``ProviderKeyError`` here means this always
    runs inside the caller's guarded try/except (see ``agent.py``), which
    funnels straight into clean provider_key_error finalization.

    ``skip_llm`` is set when a per-call llm override (mode B) is already
    present — mode B always wins over the org's llm config (see
    ``build_llm``), so decrypting/re-validating it here would be wasted
    work, and worse, could fail an otherwise-valid override call on an
    unrelated, unused org llm config problem (a stale base_url, or a key a
    ``HAIL_PROVIDER_SECRET_KEY`` rotation invalidated).
    """
    if organization_id is None:
        return {}
    async with session_scope() as session:
        rows = await load_org_provider_configs(session, organization_id)
    resolved: dict[str, ResolvedLayer] = {}
    for layer, row in rows.items():
        if layer == "llm" and skip_llm:
            continue
        api_key: str | None = None
        if row.encrypted_api_key is not None:
            api_key = provider_cipher().decrypt(row.encrypted_api_key)
        params = dict(row.params)
        if layer == "llm" and row.provider == "openai-compatible":
            stored_base_url = params.get("base_url")
            if not stored_base_url:
                raise ProviderKeyError("org llm config is missing base_url")
            try:
                params["base_url"] = await asyncio.to_thread(
                    assert_public_https_url, stored_base_url
                )
            except UnsafeUrlError as exc:
                raise ProviderKeyError(
                    f"org llm base_url is not permitted: {exc}"
                ) from exc
        resolved[layer] = ResolvedLayer(
            provider=row.provider,
            api_key=api_key,
            params=params,
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
    model = org.params.get("model", "")
    if org.provider == "openai-compatible":
        # openai-compatible has no house equivalent — base_url is inherently
        # customer-specific, so a missing org key always fails fast here.
        if org.api_key is None:
            raise ProviderKeyError("org llm config has no stored api key")
        # The resolving SSRF guard already ran (off the event loop) in
        # resolve_org_configs, which normalizes org.params["base_url"] to
        # the canonicalized, verified-public form. A legacy/malformed row
        # missing base_url would KeyError out of the ProviderKeyError
        # contract, so still fail fast explicitly on that.
        base_url = org.params.get("base_url")
        if not base_url:
            raise ProviderKeyError("org llm config is missing base_url")
        return openai_plugin.LLM(base_url=base_url, api_key=org.api_key, model=model)
    if org.provider == "anthropic":
        kwargs: dict[str, Any] = {"model": model}
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.anthropic_api_key:
            raise ProviderKeyError("no org or house anthropic key available")
        return anthropic_plugin.LLM(**kwargs)
    if org.provider == "google":
        kwargs = {"model": model}
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.google_api_key:
            raise ProviderKeyError("no org or house google key available")
        return google_plugin.LLM(**kwargs)
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


def _house_tts(
    voice_id_override: str | None, language: str | None
) -> list[agents_tts.TTS]:
    instances: list[agents_tts.TTS] = []
    if settings.cartesia_api_key:
        kwargs: dict[str, Any] = {
            "model": settings.cartesia_model,
            "voice": voice_id_override or settings.cartesia_voice_id,
        }
        if language:
            kwargs["language"] = language
        instances.append(cartesia_plugin.TTS(**kwargs))
    if settings.eleven_api_key:
        kwargs = {
            "voice_id": voice_id_override or settings.elevenlabs_voice_id,
            "model": settings.elevenlabs_model,
        }
        if language:
            kwargs["language"] = language
        instances.append(elevenlabs_plugin.TTS(**kwargs))
    return instances


def _org_tts(
    org: ResolvedLayer, voice_id_override: str | None, language: str | None
) -> agents_tts.TTS:
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
        if language:
            kwargs["language"] = language
        return cartesia_plugin.TTS(**kwargs)
    if org.provider == "elevenlabs":
        kwargs = {"model": org.params.get("model") or settings.elevenlabs_model}
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.eleven_api_key:
            raise ProviderKeyError("no org or house elevenlabs key available")
        if voice:
            kwargs["voice_id"] = voice
        if language:
            kwargs["language"] = language
        return elevenlabs_plugin.TTS(**kwargs)
    raise ProviderKeyError(f"unknown org tts provider '{org.provider}'")


def build_tts(
    org: ResolvedLayer | None = None,
    voice_id_override: str | None = None,
    language: str | None = None,
) -> agents_tts.TTS:
    """Construct the TTS for one call.

    ``org`` present (mode C): the org's provider is used, with Hail's house
    TTS instances appended as a fallback only if ``org.fallback_enabled``.
    ``org`` absent (mode A): Cartesia primary, ElevenLabs fallback — a
    provider is included only when its API key is configured, so a
    single-key self-host still works (tenet 4). With both keys set the two
    are wrapped in a ``FallbackAdapter`` with Cartesia first. With one key
    set that provider is used directly with no adapter.

    ``language`` is the per-call ``voice_config.language`` (ISO 639-1); it is
    applied to every instance built here — BYO and house fallbacks alike —
    so a provider failover never silently switches the call's language back
    to English. ``None`` keeps each plugin's default (English).
    """
    if org is not None:
        byo = _org_tts(org, voice_id_override, language)
        if org.fallback_enabled:
            house = _house_tts(voice_id_override, language)
            if house:
                return agents_tts.FallbackAdapter([byo, *house])
        return byo
    instances = _house_tts(voice_id_override, language)
    if not instances:
        raise RuntimeError(
            "No TTS provider configured: set CARTESIA_API_KEY or ELEVEN_API_KEY."
        )
    if len(instances) == 1:
        return instances[0]
    return agents_tts.FallbackAdapter(instances)


def build_stt(
    org: ResolvedLayer | None = None,
    language: str | None = None,
    provider: str = "deepgram",
    stt_drives_turns: bool = False,
) -> agents_stt.STT:
    """Construct the STT for one call.

    ``provider`` arrives already resolved (per-call pin > org BYO row >
    language auto-route — ``resolve_stt_provider``). The org row is used
    only when its provider matches ``provider``; a row pinned away by the
    per-call choice is ignored rather than billed. ``stt_drives_turns``
    is set when the session's turn detection is ``"stt"`` — Speechmatics
    then runs its ADAPTIVE end-of-utterance mode instead of EXTERNAL.

    An org row on a provider outside ``("deepgram", "speechmatics")`` fails
    fast with ``ProviderKeyError`` (matching ``_org_llm``/``_org_tts``)
    rather than silently billing Hail's key for a BYO org.

    Deepgram fallback semantics are unchanged: BYO + fallback_enabled
    appends the house instance. Speechmatics mirrors them.
    """
    if org is not None and org.provider not in ("deepgram", "speechmatics"):
        raise ProviderKeyError(f"unknown org stt provider '{org.provider}'")

    org_matches = org is not None and org.provider == provider
    if provider == "speechmatics":
        kwargs: dict[str, Any] = {
            "language": language or "en",
            "operating_point": "enhanced",
        }
        if stt_drives_turns:
            kwargs["turn_detection_mode"] = (
                speechmatics_plugin.TurnDetectionMode.ADAPTIVE
            )
        if org_matches and org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.speechmatics_api_key:
            raise ProviderKeyError("no org or house speechmatics key available")
        byo = speechmatics_plugin.STT(**kwargs)
        if org_matches and org.fallback_enabled and settings.speechmatics_api_key:
            house_kwargs = dict(kwargs)
            house_kwargs.pop("api_key", None)
            return agents_stt.FallbackAdapter(
                [byo, speechmatics_plugin.STT(**house_kwargs)]
            )
        return byo

    house_kwargs: dict[str, Any] = {"model": settings.deepgram_model}
    if language:
        house_kwargs["language"] = language
    if org_matches:
        kwargs = {"model": org.params.get("model") or settings.deepgram_model}
        if language:
            kwargs["language"] = language
        if org.api_key is not None:
            kwargs["api_key"] = org.api_key
        elif not settings.deepgram_api_key:
            raise ProviderKeyError("no org or house deepgram key available")
        byo = deepgram_plugin.STT(**kwargs)
        if org.fallback_enabled and settings.deepgram_api_key:
            return agents_stt.FallbackAdapter(
                [byo, deepgram_plugin.STT(**house_kwargs)]
            )
        return byo
    return deepgram_plugin.STT(**house_kwargs)


def build_session(
    llm_cfg: dict[str, Any] | None,
    vad: agents_vad.VAD,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    voice_id_override: str | None = None,
    language: str | None = None,
    stt_choice: str = "auto",
) -> AgentSession:
    """Build the :class:`AgentSession` for one job.

    ``vad`` is the per-process Silero instance loaded once in
    :func:`hailhq.voicebot.agent.prewarm`. ``org_cfgs`` is the org's resolved
    BYO layers (from :func:`resolve_org_configs`); ``voice_id_override`` and
    ``language`` are the per-call ``voice_config.voice_id`` /
    ``voice_config.language`` from dispatch metadata — the voice beats any
    org-level default, the language pins both STT and TTS.

    ``stt_choice`` is the per-call ``voice_config.stt`` ("auto" routes by
    language). Provider resolution: per-call pin > org BYO row > auto.
    Auto only picks speechmatics when a key exists for it (org or house)
    — a deepgram-only self-host keeps working (tenet 4). Turn detection:
    semantic MultilingualModel for its 14 languages, "stt" when
    speechmatics serves the call, "vad" as the floor.

    ``language`` arrives raw from dispatch metadata (a direct LiveKit
    dispatch can carry any string, bypassing the API's request-validation
    gate) — a code outside :data:`SUPPORTED_LANGUAGES` is degraded to
    ``None`` (provider defaults / English) rather than raising, so a
    malformed dispatch never crashes the call.
    """
    if language is not None and language not in SUPPORTED_LANGUAGES:
        logger.warning(
            "unsupported language %r from dispatch metadata; falling back to "
            "provider defaults",
            language,
        )
        language = None
    org_cfgs = org_cfgs or {}
    org_stt = org_cfgs.get("stt")
    provider = resolve_stt_provider(
        stt_choice, org_stt.provider if org_stt else None, language
    )
    if language is not None and provider not in SUPPORTED_LANGUAGES[language].stt:
        # Direct dispatch can bypass the API's 422 gate; deepgram covers
        # every supported language, so degrade rather than fail the call.
        provider = "deepgram"
    if (
        provider == "speechmatics"
        and stt_choice == "auto"
        and not settings.speechmatics_api_key
        and not (org_stt and org_stt.provider == "speechmatics" and org_stt.api_key)
    ):
        provider = "deepgram"
    mode = turn_mode_for(language, provider)
    turn_detection: Any = MultilingualModel() if mode == "semantic" else mode
    return AgentSession(
        vad=vad,
        stt=build_stt(org_stt, language, provider, stt_drives_turns=(mode == "stt")),
        tts=build_tts(org_cfgs.get("tts"), voice_id_override, language),
        llm=build_llm(llm_cfg, org_cfgs.get("llm")),
        turn_detection=turn_detection,
    )
