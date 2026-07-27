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
   no safe capability probe.
3. EMPIRICALLY VERIFY the ordering before trusting a capability probe: a bad
   key must return 401/403, and a valid key with bad params must return a
   post-auth 4xx (404/400/422). If auth is NOT checked before params, the
   ``capability`` mode would mark a bad key valid — use ``auth`` mode instead.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Literal

import httpx
from hailhq.core.url_guard import UnsafeUrlError, assert_public_https_url
from hailhq.core.urls import join_url

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
                "model_id": "sonic-2",
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


async def validate_provider_key(
    layer: str,
    provider: str,
    api_key: str,
    params: dict,
    client: httpx.AsyncClient | None = None,
) -> tuple[ValidationStatus, str]:
    """Return (status, message). Never raises on provider/network failure."""
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
