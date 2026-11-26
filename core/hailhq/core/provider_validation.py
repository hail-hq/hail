"""Live provider-key validation: capability probe + tri-state result.

Triggered only by the explicit internal ``/validate`` route (the console
TEST KEY button); PUT is a pure store. For TTS/STT we probe the actual
capability endpoint with deliberately-invalid params so the key's real
permission is exercised WITHOUT running (or billing) the operation — a
key scoped for the wrong permission (common with ElevenLabs/Cartesia/
Deepgram granular keys) is caught here rather than at call time. LLM
providers keep an auth probe (GET /models): capability probes for them
are designed but unverified pending test keys — a KNOWN GAP.

Result is tri-state: 'valid' | 'invalid' | 'indeterminate'. A transient
429/5xx/network is 'indeterminate' ("couldn't verify"), never 'invalid'.

Adding a provider (this does NOT auto-scale — each needs a hand-written,
verified probe):

1. Add it to the params model for its layer in ``provider_config.py``.
2. Add a ``_probe_for`` branch here. Prefer a ``capability`` probe (call the
   real endpoint with deliberately-bad params so nothing runs/bills); fall
   back to an ``auth`` probe (a cheap authenticated GET) if the provider has
   no safe capability probe. PCM adapters synthesize one fixed word when the user selects Test key.
3. EMPIRICALLY VERIFY the ordering before trusting a capability probe: a bad
   key must return 401/403, and a valid key with bad params must return a
   post-auth 4xx (404/400/422). If auth is NOT checked before params, the
   ``capability`` mode would mark a bad key valid — use ``auth`` mode instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
from contextlib import aclosing
from typing import Literal

import httpx
from hailhq.core.config import settings
from hailhq.core.provider_config import provider_cipher
from hailhq.core.providers.tts import PCM_ADAPTERS, GeminiSpeech
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
from hailhq.core.urls import join_url
from hailhq.core.voice_catalog import validate_voice_language

ValidationStatus = Literal["valid", "invalid", "indeterminate"]

_TIMEOUT = httpx.Timeout(10.0)
_CARTESIA_VERSION = "2024-06-10"
_ANTHROPIC_VERSION = "2023-06-01"

# Sentinels: obviously-invalid ids so TTS providers 404 before synthesizing.
_SENTINEL_VOICE = "00000000000000000000000000000000"
_SENTINEL_VOICE_UUID = "00000000-0000-0000-0000-000000000000"


@dataclasses.dataclass(frozen=True)
class _Probe:
    method: str
    url: str
    headers: dict[str, str]
    json_body: dict | None
    mode: Literal["auth", "capability"]


def _probe_for(provider: str, api_key: str, params: dict) -> _Probe | None:
    if provider == "openai-compatible":
        base_url = params.get("base_url") or ""
        if not base_url:
            return None
        try:
            base_url = assert_public_https_url(base_url)
        except UnsafeUrlError:
            return None
        return _Probe(
            "GET",
            join_url(base_url, "models"),
            {"Authorization": f"Bearer {api_key}"},
            None,
            "auth",
        )
    if provider == "anthropic":
        return _Probe(
            "GET",
            "https://api.anthropic.com/v1/models",
            {"x-api-key": api_key, "anthropic-version": _ANTHROPIC_VERSION},
            None,
            "auth",
        )
    if provider == "google":
        return _Probe(
            "GET",
            f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}",
            {},
            None,
            "auth",
        )
    if provider == "elevenlabs":
        return _Probe(
            "POST",
            f"https://api.elevenlabs.io/v1/text-to-speech/{_SENTINEL_VOICE}",
            {"xi-api-key": api_key},
            {"text": ".", "model_id": "eleven_multilingual_v2"},
            "capability",
        )
    if provider == "cartesia":
        return _Probe(
            "POST",
            "https://api.cartesia.ai/tts/bytes",
            {"X-API-Key": api_key, "Cartesia-Version": _CARTESIA_VERSION},
            {
                "model_id": "sonic-3.6",
                "transcript": ".",
                "voice": {"mode": "id", "id": _SENTINEL_VOICE_UUID},
                "output_format": {
                    "container": "raw",
                    "encoding": "pcm_s16le",
                    "sample_rate": 8000,
                },
            },
            "capability",
        )
    if provider == "deepgram":
        return _Probe(
            "POST",
            "https://api.deepgram.com/v1/listen",
            {"Authorization": f"Token {api_key}"},
            {},
            "capability",
        )
    if provider == "speechmatics":
        # Auth probe: the batch jobs listing is a cheap authenticated GET;
        # 200 proves the key, 401/403 disproves it.
        return _Probe(
            "GET",
            "https://asr.api.speechmatics.com/v2/jobs",
            {"Authorization": f"Bearer {api_key}"},
            None,
            "auth",
        )
    return None


def _classify(mode: str, status: int) -> ValidationStatus:
    if status in (401, 403):
        return "invalid"
    if mode == "auth":
        return "valid" if status in (200, 206) else "indeterminate"
    # capability: we sent bad params, so a 2xx or a post-auth 4xx (not auth/
    # ratelimit) proves the key authenticated AND had the capability permission.
    # A 3xx redirect proves neither, so it stays indeterminate.
    if (200 <= status < 300) or (400 <= status < 500 and status not in (401, 403, 429)):
        return "valid"
    return "indeterminate"


async def validate_tts_options(
    provider: str, params: dict, api_key: str | None, *, has_stored_key: bool
) -> None:
    adapter = PCM_ADAPTERS.get(provider)
    if adapter is None:
        return
    if api_key is not None and not api_key.strip():
        raise ValueError("Enter an API key.")
    has_workspace_key = api_key is not None or has_stored_key
    adapter.validate_options(params, has_workspace_key=has_workspace_key)
    if not has_workspace_key and not await adapter.house_credentials_available():
        raise ValueError(f"No Hail credentials are available for {provider} speech.")


def supports_saved_key_draft_test(layer: str, provider: str) -> bool:
    """Only a fixed vendor endpoint may receive a stored key for draft settings."""
    return layer == "tts" and provider == GeminiSpeech.provider


async def validate_tts_voice_selection(
    provider: str,
    params: dict,
    api_key: str | None,
    stored_key: str | None,
    *,
    saved_voice_id: str | None,
) -> None:
    adapter = PCM_ADAPTERS.get(provider)
    if adapter is not None:
        if params.get("voice_id") not in (
            None,
            "",
            adapter.default_voice,
            saved_voice_id,
        ):
            raise ValueError("Choose a suggested voice or update Advanced.")
        return

    if api_key is None and stored_key is not None:
        api_key = provider_cipher().decrypt(stored_key)
    if provider == "cartesia":
        key = settings.cartesia_api_key
        model = settings.cartesia_model
        voice = settings.cartesia_voice_id
    else:
        key = settings.eleven_api_key
        model = settings.elevenlabs_model
        voice = settings.elevenlabs_voice_id
    await validate_voice_language(
        provider,
        params.get("voice_id") or voice,
        params.get("model") or model,
        key if api_key is None else api_key,
        params["language"],
    )


async def validate_provider_key(
    layer: str,
    provider: str,
    api_key: str,
    params: dict,
    client: httpx.AsyncClient | None = None,
) -> tuple[ValidationStatus, str]:
    """Return (status, message). Never raises on provider/network failure."""
    if layer == "tts" and provider in PCM_ADAPTERS:
        if client is not None:
            return await _validate_pcm_adapter(provider, api_key, params, client)
        async with httpx.AsyncClient(timeout=_TIMEOUT) as speech_client:
            return await _validate_pcm_adapter(provider, api_key, params, speech_client)
    probe = await asyncio.to_thread(_probe_for, provider, api_key, params)
    if probe is None:
        if provider == "openai-compatible" and params.get("base_url"):
            return "invalid", "base_url is not a permitted public https endpoint"
        return "invalid", f"unknown provider '{provider}' for layer '{layer}'"

    owns_client = client is None
    if owns_client:
        client = httpx.AsyncClient(timeout=_TIMEOUT)
    try:
        resp = await client.request(
            probe.method, probe.url, headers=probe.headers, json=probe.json_body
        )
    except httpx.HTTPError as exc:
        return (
            "indeterminate",
            f"couldn't reach {provider} ({exc.__class__.__name__}) — try again",
        )
    finally:
        if owns_client:
            await client.aclose()

    status = _classify(probe.mode, resp.status_code)
    if status == "valid":
        return status, "ok"
    if status == "invalid":
        return status, f"{provider} rejected the key (HTTP {resp.status_code})"
    return (
        status,
        f"couldn't verify with {provider} (HTTP {resp.status_code}) — try again",
    )


async def _validate_pcm_adapter(
    provider: str, api_key: str, params: dict, client: httpx.AsyncClient
) -> tuple[ValidationStatus, str]:
    # The explicit Test key action synthesizes one fixed word. Authentication
    # alone cannot prove the project can use the selected speech model.
    try:
        speech = PCM_ADAPTERS[provider].from_config(params, api_key)
        async with aclosing(speech.pcm(client, "Hello.")) as audio:
            async for chunk in audio:
                if chunk:
                    return "valid", "ok"
    except httpx.HTTPStatusError as exc:
        status = exc.response.status_code
        if status in (400, 401, 403, 404, 422):
            return (
                "invalid",
                f"{provider} rejected the speech configuration (HTTP {status})",
            )
        return (
            "indeterminate",
            f"couldn't verify with {provider} (HTTP {status}) — try again",
        )
    except httpx.HTTPError:
        return "indeterminate", f"couldn't reach {provider} — try again"
    except ValueError as exc:
        return "invalid", str(exc)
    return "indeterminate", "The provider did not return test audio. Try again."
