"""Voice-pipeline assembly for the Hail voicebot worker.

Centralizes the ``AgentSession`` construction so :mod:`hailhq.voicebot.agent`
stays focused on lifecycle (connect, parse metadata, attach event handlers,
clean up). Two LLM modes are supported, switched on the dispatch metadata's
``llm`` field:

* **Mode A — Hail fallback chain** (``llm`` is ``None``): assemble a
  :class:`livekit.agents.llm.FallbackAdapter` over OpenAI → Google → Anthropic
  fast-tier models so a single provider outage doesn't take the call down.
* **Mode B — caller-provided endpoint** (``llm`` is a dict with
  ``base_url``/``api_key``/``model``): a single
  :class:`livekit.plugins.openai.LLM` pointed at the OpenAI-compatible endpoint
  the caller supplied. No fallback — the caller chose this brain explicitly.

API surface verified 2026-04-28 against:

* ``livekit-agents/livekit/agents/voice/agent_session.py`` (AgentSession init).
* ``livekit-agents/livekit/agents/llm/fallback_adapter.py`` (FallbackAdapter).
* ``livekit-plugins/livekit-plugins-openai/livekit/plugins/openai/llm.py``
  (the openai plugin's LLM accepts ``base_url``/``api_key``/``model``).
"""

from __future__ import annotations

from typing import Any

from livekit.agents import AgentSession
from livekit.agents import llm as agents_llm
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


def build_llm(llm_cfg: dict[str, Any] | None) -> agents_llm.LLM:
    """Construct the LLM for one call.

    ``llm_cfg`` is the ``llm`` field of the dispatch metadata. ``None`` means
    "use the Hail fallback chain (mode A)"; a dict means
    "the caller pinned an OpenAI-compatible endpoint (mode B)".
    """
    if llm_cfg is None:
        # Mode A — fallback chain. ``attempt_timeout`` and ``retry_interval``
        # are the upstream defaults; ``max_retry_per_llm=1`` keeps a brief
        # retry per provider before failing over.
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

    return openai_plugin.LLM(
        base_url=llm_cfg["base_url"],
        api_key=llm_cfg["api_key"],
        model=llm_cfg["model"],
    )


def build_tts() -> agents_tts.TTS:
    """Construct the TTS for one call: Cartesia primary, ElevenLabs fallback.

    A provider is included only when its API key is configured, so a
    single-key self-host still works (tenet 4). With both keys set the two are
    wrapped in a ``FallbackAdapter`` with Cartesia first — the fixed
    provider ordering mirrors the hardcoded LLM fallback chain; retry/timeout
    tuning is left at the SDK defaults for v1. With one key set that provider is used
    directly with no adapter. The order is fixed in code, not caller- or
    env-selectable.
    """
    instances: list[agents_tts.TTS] = []
    if settings.cartesia_api_key:
        instances.append(
            cartesia_plugin.TTS(
                model=settings.cartesia_model,
                voice=settings.cartesia_voice_id,
            )
        )
    if settings.eleven_api_key:
        instances.append(
            elevenlabs_plugin.TTS(
                voice_id=settings.elevenlabs_voice_id,
                model=settings.elevenlabs_model,
            )
        )
    if not instances:
        raise RuntimeError(
            "No TTS provider configured: set CARTESIA_API_KEY or ELEVEN_API_KEY."
        )
    if len(instances) == 1:
        return instances[0]
    return agents_tts.FallbackAdapter(instances)


def build_session(
    llm_cfg: dict[str, Any] | None,
    vad: agents_vad.VAD,
) -> AgentSession:
    """Build the :class:`AgentSession` for one job.

    ``vad`` is the per-process Silero instance loaded once in
    :func:`hailhq.voicebot.agent.prewarm`.
    """
    return AgentSession(
        vad=vad,
        stt=deepgram_plugin.STT(model=settings.deepgram_model),
        tts=build_tts(),
        llm=build_llm(llm_cfg),
    )


__all__ = ["build_llm", "build_tts", "build_session"]
