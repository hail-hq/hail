"""Voice-pipeline assembly for the Hail voicebot worker.

Centralizes the ``AgentSession`` construction so :mod:`hailhq.voicebot.agent`
stays focused on lifecycle (connect, parse metadata, attach event handlers,
clean up). Three LLM modes are supported:

* **Mode A, Hail fallback chain** (``llm`` is ``None`` and no org BYO llm
  config): assemble a :class:`livekit.agents.llm.FallbackAdapter` over
  Anthropic (on Bedrock when ``ANTHROPIC_BEDROCK_REGION`` is set), its
  backup model, OpenAI, then Gemini, so a single provider outage doesn't
  take the call down.
* **Mode B, caller-provided endpoint** (dispatch metadata's ``llm`` is a
  dict with ``base_url``/``api_key``/``model``, possibly decrypted from
  ``api_key_enc``): a single :class:`livekit.plugins.openai.LLM` pointed at
  the OpenAI-compatible endpoint the caller supplied. No fallback, the
  caller chose this brain explicitly. Takes precedence over mode C.
* **Mode C, per-org BYO provider config** (``resolve_org_configs`` finds a
  row for the org): the org's own key/provider is used for the layer, with
  Hail's own key used as a fallback only if the org opted in
  (``fallback_enabled``). Absent a per-call override, this is the org's
  standing choice.

Precedence per layer: per-call (mode B; LLM only, TTS/STT have no per-call
override) > org BYO (mode C) > house default (mode A). STT provider
selection is console-BYO-only: the org's BYO row (mode C) wins, else the
call auto-routes by language (see ``hailhq.core.languages``). A BYO layer
that cannot be built (no key, unknown provider, unsafe URL) and has
fallback disabled raises :class:`ProviderKeyError`, deliberately
fail-fast, since silently substituting Hail's keys would defeat the point
of BYO unless the org opted into the fallback.

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

Speechmatics 1.8.8 uses Agent STT's Linden-1 model. Legacy operating-point
inputs normalize to that model; the previous service's enhanced/standard
settings no longer apply. Audio turn detection uses the SDK's built-in
inference.TurnDetector, with STT/VAD fallback outside its supported languages.
Verified against current docs on 2026-10-18:
https://docs.livekit.io/agents/models/stt/speechmatics/
https://docs.livekit.io/agents/logic/turns/turn-detector/
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
from collections.abc import AsyncIterable
from typing import Any
from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.languages import (
    SUPPORTED_LANGUAGES,
    default_stt_for,
    elevenlabs_language,
    resolve_stt_provider,
    tts_providers_for,
    turn_mode_for,
)
from hailhq.core.provider_config import load_org_provider_configs, provider_cipher
from hailhq.core.providers.llm.bedrock import bedrock_client, is_bedrock_client
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
from livekit.agents import (
    AgentSession,
    APIConnectOptions,
    TurnHandlingOptions,
    inference,
)
from livekit.agents import llm as agents_llm
from livekit.agents import stt as agents_stt
from livekit.agents import tts as agents_tts
from livekit.agents import vad as agents_vad
from livekit.agents.llm.fallback_adapter import (
    DEFAULT_FALLBACK_API_CONNECT_OPTIONS,
    FallbackLLMStream,
)
from livekit.agents.types import NOT_GIVEN, NotGivenOr
from livekit.agents.utils import is_given
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

logger = logging.getLogger("hailhq.voicebot")

__all__ = [
    "ProviderKeyError",
    "ResolvedLayer",
    "build_llm",
    "build_session",
    "build_stt",
    "build_tts",
    "decrypt_llm_metadata",
    "house_llm",
    "org_llm",
    "resolve_org_configs",
    "startup_capability_warnings",
    "stt_takes_keyterms",
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
    rather than at LLM-build time in ``org_llm``: a stored base_url could
    have been written before the guard existed, or DNS could have
    re-pointed since. Raising ``ProviderKeyError`` here means this always
    runs inside the caller's guarded try/except (see ``agent.py``), which
    funnels straight into clean provider_key_error finalization.

    ``skip_llm`` is set when a per-call llm override (mode B) is already
    present, mode B always wins over the org's llm config (see
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


# How each model turns thinking off. Sonnet 5.5 rejects ``disabled`` and
# takes ``between_tools``; Opus 5.5 cannot turn it off (None leaves it on).
# Keys are first-party ids; ``_base_model_id`` maps a Bedrock id onto them.
_THINKING_OFF: dict[str, dict[str, str] | None] = {
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-opus-5-5": None,
}


def _base_model_id(model: str) -> str:
    """``eu.anthropic.claude-sonnet-5-5-v1:0`` -> ``claude-sonnet-5-5``: drop
    the region prefix, ``anthropic.``, a date (``-20260101``) and the
    version suffix."""
    base = model.rsplit("anthropic.", 1)[-1]
    return re.sub(r"(-\d{8})?(-v\d+)?(:\d+)?$", "", base)


_warned_models: set[str] = set()


def _thinking_off(model: str) -> dict[str, str] | None:
    """The model's thinking-off setting. An id missing from the table gets
    ``disabled`` and one warning: a check must never stop the voicebot."""
    base = _base_model_id(model)
    if base in _THINKING_OFF:
        return _THINKING_OFF[base]
    if base not in _warned_models:
        _warned_models.add(base)
        logger.warning(
            "model %r has no entry in _THINKING_OFF "
            "(voicebot/hailhq/voicebot/pipeline.py); using thinking disabled",
            model,
        )
    return {"type": "disabled"}


class _FastAnthropicLLM(anthropic_plugin.LLM):
    """Thinking off where the model allows it, and low effort on every
    request: a voice turn needs the first word fast, and the call's turns
    are short. Sonnet 5.5 still thinks between tool calls; the plugin drops
    those thinking blocks from the history, which Anthropic accepts
    (docs/operations/voice-llm.md)."""

    async def _prewarm_impl(self) -> None:
        # The plugin prewarms with models.list, which the Bedrock client
        # does not have.
        if is_bedrock_client(self._client):
            return
        await super()._prewarm_impl()

    def chat(self, *, extra_kwargs: Any = NOT_GIVEN, **kwargs: Any) -> Any:
        extra = dict(extra_kwargs) if is_given(extra_kwargs) else {}
        thinking = _thinking_off(self.model)
        if thinking is not None:
            extra.setdefault("thinking", thinking)
        extra.setdefault("output_config", {"effort": "low"})
        return super().chat(extra_kwargs=extra, **kwargs)


def house_anthropic_ready() -> bool:
    """Bedrock signs with the AWS keys; otherwise Anthropic's API key."""
    return bool(settings.anthropic_bedrock_region or settings.anthropic_api_key)


def house_anthropic(model: str) -> agents_llm.LLM:
    if not settings.anthropic_bedrock_region:
        return _FastAnthropicLLM(model=model)
    # The plugin insists on a key even when handed a client; Bedrock signs
    # with the AWS credential chain instead.
    return _FastAnthropicLLM(
        model=model,
        client=bedrock_client(settings.anthropic_bedrock_region),
        api_key="bedrock",
    )


def house_llm(provider: str, model: str, **kwargs: Any) -> agents_llm.LLM | None:
    """One house LLM client on Hail's key, or None when its model id or key
    is missing, so a self-host with only some keys still gets a chain.
    ``provider`` is ``anthropic``, ``openai`` or ``google``; ``kwargs`` go to
    the Google client."""
    if not model:
        return None
    if provider == "anthropic":
        return house_anthropic(model) if house_anthropic_ready() else None
    if provider == "openai":
        if not settings.openai_api_key:
            return None
        return openai_plugin.LLM(model=model, api_key=settings.openai_api_key)
    if not settings.google_api_key:
        return None
    return google_plugin.LLM(model=model, api_key=settings.google_api_key, **kwargs)


def _house_llms() -> list[agents_llm.LLM]:
    """Sonnet 5.5, then Haiku 5.5, then OpenAI: the order that passed the
    replayed hard turns in docs/operations/voice-llm.md. Gemini is the last
    backup."""
    llms = [
        house_llm("anthropic", settings.anthropic_model),
        house_llm("anthropic", settings.anthropic_backup_model),
        house_llm("openai", settings.openai_model),
        house_llm(
            "google",
            settings.google_model,
            thinking_config={"thinking_level": "minimal"},
        ),
    ]
    return [llm for llm in llms if llm is not None]


def _after_repeat(sent: str, new: str) -> int | None:
    """Index in ``new`` just past its repeat of ``sent``, ignoring case and
    whitespace. -1 when ``new`` does not repeat ``sent``; None when ``new``
    is still too short to tell."""
    i = 0
    for ch in sent:
        if ch.isspace():
            continue
        while i < len(new) and new[i].isspace():
            i += 1
        if i == len(new):
            return None
        if new[i].lower() != ch.lower():
            return -1
        i += 1
    return i


class _ToolCallSent(BaseException):
    """Stops the chain when a model fails after it sent a tool call. A
    BaseException, so ``FallbackLLMStream._run`` lets it through: no
    hand-over, and no "retrying" log for a retry that does not happen."""


class _CallLLMStream(FallbackLLMStream):
    """One call reply over the chain (docs/operations/voice-llm.md):

    * After a tool call was sent, a model that fails ends the reply. The
      tool already runs, so the next model must not send it again.
    * After text was sent, the next model restarts the reply. Its repeat of
      the words already sent is dropped, so they are said once.
    * When every model failed, the session reruns the chain (its
      ``max_retry``) only if nothing was sent yet. The adapter's
      ``retry_on_chunk_sent`` still lets one model hand over to the next."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Read by LLMStream._main_task (llm/llm.py): an error after output
        # was sent is not retried. FallbackLLMStream copies the adapter's
        # True here; the hand-over reads the adapter's own flag.
        self._retry_on_chunk_sent = False
        self._said = ""
        # Trailing spaces go out with the next text, so a model that takes
        # over decides the space after the words said ("Aw" + ", great.").
        self._space = ""
        self._tool_call_sent = False
        self._failed: tuple[agents_llm.LLM, Exception] | None = None

    async def _run(self) -> None:
        try:
            await super()._run()
            return
        except _ToolCallSent:
            assert self._failed is not None
            llm, error = self._failed
        # What the adapter does for a failed model: mark it down until a
        # background request succeeds (fallback_adapter.py, _run).
        adapter = self._fallback_adapter
        status = adapter._status[adapter._llm_instances.index(llm)]
        status.selection_id = 0
        if status.available:
            status.available = False
            adapter.emit(
                "llm_availability_changed",
                agents_llm.AvailabilityChangedEvent(llm=llm, available=False),
            )
        self._try_recovery(llm)
        # LiveKit already logged "switching to next LLM" in _try_generate.
        logger.warning(
            "%s failed after a tool call was sent; no hand-over, "
            "no switch to the next LLM",
            llm.label,
        )
        # Raised outside the except block, so its cause and context stay.
        raise error

    def _out(self, content: str) -> str:
        text = self._space + content
        body = text.rstrip()
        self._space = text[len(body) :]
        self._said += body
        return body

    async def _try_generate(
        self, *, llm: agents_llm.LLM, check_recovery: bool = False
    ) -> AsyncIterable[agents_llm.ChatChunk]:
        if check_recovery:
            async for chunk in super()._try_generate(llm=llm, check_recovery=True):
                yield chunk
            return
        # The failed model's trailing spaces are not said.
        self._space = ""
        restart = bool(self._said)
        pending = "" if restart else None
        try:
            async for chunk in super()._try_generate(llm=llm):
                delta = chunk.delta
                if delta and (delta.content or pending):
                    rest = delta.content or ""
                    if pending is not None:
                        pending += rest
                        end = _after_repeat(self._said, pending)
                        if end is None:
                            # So far only a repeat of the words said: hold
                            # it. The chunk still goes out, for its usage,
                            # extra or tool call.
                            rest = ""
                        elif end == -1:
                            # Not a repeat: keep it, apart from the words said.
                            # A space held from before a tool call counts.
                            gap = pending[:1].isspace() or bool(self._space)
                            rest = pending if gap else " " + pending
                            pending = None
                        else:
                            rest = pending[end:]
                            pending = None
                    delta = delta.model_copy(
                        update={"content": self._out(rest) or None}
                    )
                    chunk = chunk.model_copy(update={"delta": delta})
                if delta and delta.tool_calls:
                    self._tool_call_sent = True
                    if restart:
                        # A tool call drops a held repeat; the text after it
                        # is matched and spaced again.
                        pending = ""
                yield chunk
        except Exception as e:
            if not self._tool_call_sent:
                raise
            self._failed = (llm, e)
            raise _ToolCallSent() from None


class _CallFallbackAdapter(agents_llm.FallbackAdapter):
    def chat(
        self,
        *,
        chat_ctx: agents_llm.ChatContext,
        tools: list[agents_llm.Tool] | None = None,
        conn_options: APIConnectOptions = DEFAULT_FALLBACK_API_CONNECT_OPTIONS,
        parallel_tool_calls: NotGivenOr[bool] = NOT_GIVEN,
        tool_choice: NotGivenOr[agents_llm.ToolChoice] = NOT_GIVEN,
        extra_kwargs: NotGivenOr[dict[str, Any]] = NOT_GIVEN,
    ) -> agents_llm.LLMStream:
        return _CallLLMStream(
            llm=self,
            conn_options=conn_options,
            chat_ctx=chat_ctx,
            tools=tools or [],
            parallel_tool_calls=parallel_tool_calls,
            tool_choice=tool_choice,
            extra_kwargs=extra_kwargs,
        )


def _fallback_chain(
    llms: list[agents_llm.LLM], *, retry_on_chunk_sent: bool
) -> agents_llm.LLM:
    # One try per model, no retry: a failed model hands over to the next at
    # once. ``retry_on_chunk_sent`` is for calls: a model that fails after it
    # sent text hands over too (docs/operations/voice-llm.md).
    adapter = (
        _CallFallbackAdapter if retry_on_chunk_sent else agents_llm.FallbackAdapter
    )
    return adapter(
        llm=llms,
        attempt_timeout=10.0,
        max_retry_per_llm=0,
        retry_on_chunk_sent=retry_on_chunk_sent,
    )


def _house_llm(*, retry_on_chunk_sent: bool) -> agents_llm.LLM:
    llms = _house_llms()
    if not llms:
        raise ProviderKeyError("no house LLM has a model and a key")
    return _fallback_chain(llms, retry_on_chunk_sent=retry_on_chunk_sent)


def org_llm(org: ResolvedLayer) -> agents_llm.LLM:
    model = org.params.get("model", "")
    if org.provider == "openai-compatible":
        # openai-compatible has no house equivalent, base_url is inherently
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
    llm_cfg: dict[str, Any] | None,
    org: ResolvedLayer | None = None,
    *,
    retry_on_chunk_sent: bool = False,
) -> agents_llm.LLM:
    """Precedence: per-call llm (mode B) > org BYO (mode C) > house chain (mode A).

    ``retry_on_chunk_sent`` is True for calls: a model that fails mid-reply
    hands over to the next one. Texts keep it False, so a failed reply is
    retried whole instead of carrying the failed model's first words."""
    if llm_cfg is not None:
        return openai_plugin.LLM(
            base_url=llm_cfg["base_url"],
            api_key=llm_cfg["api_key"],
            model=llm_cfg["model"],
        )
    if org is not None:
        byo = org_llm(org)
        if org.fallback_enabled:
            return _fallback_chain(
                [byo, *_house_llms()], retry_on_chunk_sent=retry_on_chunk_sent
            )
        return byo
    return _house_llm(retry_on_chunk_sent=retry_on_chunk_sent)


def _house_tts(
    voice_id_override: str | None, language: str | None
) -> list[agents_tts.TTS]:
    allowed = tts_providers_for(language)
    instances: list[agents_tts.TTS] = []
    if settings.cartesia_api_key and "cartesia" in allowed:
        kwargs: dict[str, Any] = {
            "model": settings.cartesia_model,
            "voice": voice_id_override or settings.cartesia_voice_id,
        }
        if language:
            kwargs["language"] = language
        instances.append(cartesia_plugin.TTS(**kwargs))
    if settings.eleven_api_key and "elevenlabs" in allowed:
        kwargs = {
            "voice_id": voice_id_override or settings.elevenlabs_voice_id,
            "model": settings.elevenlabs_model,
        }
        if language:
            kwargs["language"] = elevenlabs_language(language)
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
            kwargs["language"] = elevenlabs_language(language)
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
    ``org`` absent (mode A): Cartesia primary, ElevenLabs fallback, a
    provider is included only when its API key is configured, so a
    single-key self-host still works (tenet 4). With both keys set the two
    are wrapped in a ``FallbackAdapter`` with Cartesia first. With one key
    set that provider is used directly with no adapter.

    ``language`` is the per-call ``voice_config.language`` (ISO 639-1); it is
    applied to every instance built here, BYO and house fallbacks alike,
    so a provider failover never silently switches the call's language back
    to English. ``None`` keeps each plugin's default (English).

    ``fallback_enabled`` on the org row is consent to house-provider
    rerouting: if the org's BYO provider can't speak ``language`` at all,
    consent means skip the incapable BYO instance and build the house
    chain only (this recurses into the ``org is None`` branch below, so it
    shares that branch's single-instance/adapter shape and its
    empty-chain ``ProviderKeyError``). Without consent it's a hard error,
    the API gate (``routes/calls.py``) is meant to catch this before the
    voicebot ever sees it; this is defense-in-depth for direct LiveKit
    dispatches that bypass the API.
    """
    if org is not None:
        if language is not None and org.provider not in tts_providers_for(language):
            if not org.fallback_enabled:
                raise ProviderKeyError(
                    f"org tts provider '{org.provider}' does not support "
                    f"language '{language}' and fallback is disabled"
                )
            logger.warning(
                "org tts provider %r does not support language %r; "
                "skipping the BYO instance and using the house chain only "
                "(fallback consented)",
                org.provider,
                language,
            )
            return build_tts(None, voice_id_override, language)
        byo = _org_tts(org, voice_id_override, language)
        if org.fallback_enabled:
            house = _house_tts(voice_id_override, language)
            if house:
                return agents_tts.FallbackAdapter([byo, *house])
        return byo
    instances = _house_tts(voice_id_override, language)
    if not instances:
        # Distinguish between no keys configured vs. language filtering all keys out.
        if settings.cartesia_api_key or settings.eleven_api_key:
            # Keys exist but the language filtered them all out. (Every
            # supported language has at least Cartesia, so `providers` is
            # never empty here.)
            providers = tts_providers_for(language)
            raise ProviderKeyError(
                f"Configured TTS providers cannot speak language '{language}'; "
                f"language requires: {', '.join(sorted(providers))}."
            )
        raise ProviderKeyError(
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

    ``provider`` arrives already resolved (org BYO row > language
    auto-route, ``resolve_stt_provider``). The org row is used only when
    its provider matches ``provider``; a row the auto-route degraded away
    from (e.g. an unsupported-language fallback) is ignored rather than
    billed. ``stt_drives_turns`` is set when the session's turn detection
    is ``"stt"``: Speechmatics then runs its VAD end-of-utterance
    mode instead of EXTERNAL.

    An org row on a provider outside ``("deepgram", "speechmatics")`` fails
    fast with ``ProviderKeyError`` (matching ``org_llm``/``_org_tts``)
    rather than silently billing Hail's key for a BYO org.

    Deepgram fallback semantics are unchanged: BYO + fallback_enabled
    appends the house instance. Speechmatics mirrors them.
    """
    if org is not None and org.provider not in ("deepgram", "speechmatics"):
        raise ProviderKeyError(f"unknown org stt provider '{org.provider}'")

    org_matches = org is not None and org.provider == provider
    if provider == "speechmatics":
        # Agents 1.8 uses Speechmatics Agent STT (Linden-1). The previous
        # service's operating points have no equivalent on the new endpoint.
        # https://docs.livekit.io/agents/models/stt/speechmatics/
        model = settings.speechmatics_model
        if not model:
            raise ProviderKeyError("SPEECHMATICS_MODEL is empty")
        if org_matches and org.params.get("operating_point"):
            logger.warning("Speechmatics operating_point is retired; using %s", model)
        if org_matches and org.params.get("model") not in (None, model):
            raise ProviderKeyError(f"Speechmatics Agent STT supports model {model}")
        kwargs: dict[str, Any] = {
            "language": language or "en",
            "model": model,
        }
        if stt_drives_turns:
            kwargs["turn_detection_mode"] = speechmatics_plugin.TurnDetectionMode.VAD
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


def stt_takes_keyterms(stt: agents_stt.STT | None) -> bool:
    """Whether the call's STT can be given keyterms (a known name).

    Deepgram takes them on Nova-3 only; an older model answers HTTP 400, and
    the session path skips the plugin's own model check. Speechmatics Agent
    STT ignores them. ``_stt_instances`` is the FallbackAdapter's wrapped
    list in livekit-agents 1.8.8; the adapter forwards session keyterms to
    each of them (``FallbackAdapter._update_session_keyterms``).
    """
    if stt is None or not stt.capabilities.keyterms:
        return False
    instances = getattr(stt, "_stt_instances", [stt])
    return all(
        s._opts.model.startswith("nova-3")
        for s in instances
        if isinstance(s, deepgram_plugin.STT)
    )


def startup_capability_warnings() -> list[str]:
    """Voicebot startup capability warnings based on configured provider keys.

    Checks for missing TTS and STT provider keys and returns warning messages
    for languages that will have degraded capability as a result.

    Returns:
        List of warning messages. Empty when all keys are configured.
    """
    warnings: list[str] = []

    # Check for unservable TTS languages when cartesia is not configured
    if not settings.cartesia_api_key:
        # Determine which TTS providers are available
        available_tts_providers = set()
        if settings.eleven_api_key:
            available_tts_providers.add("elevenlabs")

        # Find languages that have no available TTS provider
        unservable_langs = []
        for code in sorted(SUPPORTED_LANGUAGES.keys()):
            lang_tts_providers = SUPPORTED_LANGUAGES[code].tts
            if not (lang_tts_providers & available_tts_providers):
                unservable_langs.append(code)

        if unservable_langs:
            warnings.append(
                f"No Cartesia key configured; the following languages have no "
                f"usable TTS provider: {', '.join(unservable_langs)}"
            )

    # Check for speechmatics-routed languages when speechmatics is not configured
    if not settings.speechmatics_api_key:
        # Identify languages that route to speechmatics by default
        speechmatics_routed = []
        for code in sorted(SUPPORTED_LANGUAGES.keys()):
            if default_stt_for(code) == "speechmatics":
                speechmatics_routed.append(code)

        if speechmatics_routed:
            warnings.append(
                f"No Speechmatics key configured; the following languages will "
                f"fall back to Deepgram with VAD turn detection: "
                f"{', '.join(speechmatics_routed)}"
            )

    return warnings


def build_session(
    llm_cfg: dict[str, Any] | None,
    vad: agents_vad.VAD,
    org_cfgs: dict[str, ResolvedLayer] | None = None,
    voice_id_override: str | None = None,
    language: str | None = None,
) -> AgentSession:
    """Build the :class:`AgentSession` for one job.

    ``vad`` is the per-process Silero instance loaded once in
    :func:`hailhq.voicebot.agent.prewarm`. ``org_cfgs`` is the org's resolved
    BYO layers (from :func:`resolve_org_configs`); ``voice_id_override`` and
    ``language`` are the per-call ``voice_config.voice_id`` /
    ``voice_config.language`` from dispatch metadata, the voice beats any
    org-level default, the language pins both STT and TTS.

    STT provider selection is console-BYO-only (no per-call knob): the
    org's BYO STT row wins (:func:`resolve_stt_provider`), else the call
    auto-routes by language. Auto-routing only picks speechmatics when a
    key exists for it (org or house), a deepgram-only self-host keeps
    working (tenet 4). Turn detection: semantic inference.TurnDetector for its
    14 languages, "stt" when speechmatics serves the call, "vad" as the
    floor.

    ``language`` arrives raw from dispatch metadata (a direct LiveKit
    dispatch can carry any string, bypassing the API's request-validation
    gate), a code outside :data:`SUPPORTED_LANGUAGES` is degraded to
    ``None`` (provider defaults / English) rather than raising, so a
    malformed dispatch never crashes the call.
    """
    org_cfgs = org_cfgs or {}
    org_tts = org_cfgs.get("tts")
    language = language or (org_tts.params.get("language") if org_tts else None)
    if language is not None and language not in SUPPORTED_LANGUAGES:
        logger.warning(
            "unsupported language %r from dispatch metadata; falling back to "
            "provider defaults",
            language,
        )
        language = None
    org_stt = org_cfgs.get("stt")
    provider = resolve_stt_provider(org_stt.provider if org_stt else None, language)
    if language is not None and provider not in SUPPORTED_LANGUAGES[language].stt:
        # An org BYO STT row can name a provider that doesn't cover this
        # language. ``fallback_enabled`` on that row is consent to
        # house-provider rerouting: with consent, degrade to deepgram
        # (which covers every supported language); without it, this is a
        # hard error naming the provider and language, the API gate
        # (routes/calls.py) is meant to catch this before the voicebot ever
        # sees it, so this is defense-in-depth for direct LiveKit dispatches
        # that bypass the API.
        if org_stt is not None and not org_stt.fallback_enabled:
            raise ProviderKeyError(
                f"org stt provider '{provider}' does not support language "
                f"'{language}' and fallback is disabled"
            )
        logger.warning(
            "language %r resolved to stt provider %r, which does not "
            "support it; dropping %r and falling back to deepgram (turn "
            "detection degrades accordingly)",
            language,
            provider,
            provider,
        )
        provider = "deepgram"
    if (
        provider == "speechmatics"
        and not settings.speechmatics_api_key
        and not (org_stt and org_stt.provider == "speechmatics" and org_stt.api_key)
        and (org_stt is None or org_stt.fallback_enabled)
    ):
        # No speechmatics key anywhere (house or org BYO). With no org row,
        # or an org row that consented via fallback_enabled, degrade to
        # deepgram + VAD turn detection (tenet 4: a deepgram-only self-host
        # keeps working). An org row with fallback_enabled=False has NOT
        # consented to this reroute, leave provider="speechmatics" so this
        # falls through to build_stt(), which raises ProviderKeyError
        # ("no org or house speechmatics key available") instead of
        # silently substituting deepgram.
        logger.warning(
            "auto-routing picked speechmatics for language %r but no "
            "SPEECHMATICS_API_KEY is configured (house or org BYO); "
            "skipping speechmatics routing and running deepgram + VAD "
            "turn detection instead",
            language,
        )
        provider = "deepgram"
    mode = turn_mode_for(language, provider)
    turn_detection: Any = inference.TurnDetector() if mode == "semantic" else mode
    return AgentSession(
        vad=vad,
        stt=build_stt(org_stt, language, provider, stt_drives_turns=(mode == "stt")),
        tts=build_tts(org_cfgs.get("tts"), voice_id_override, language),
        llm=build_llm(llm_cfg, org_cfgs.get("llm"), retry_on_chunk_sent=True),
        turn_handling=TurnHandlingOptions(
            turn_detection=turn_detection,
            # Only the setting that differs from the SDK defaults: a transcribed
            # word, not VAD energy alone, is needed to interrupt the agent.
            interruption={"min_words": 1},
        ),
    )
