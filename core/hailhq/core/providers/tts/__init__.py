"""TTS adapters used by the call worker and voice previews."""

from uuid import UUID

from hailhq.core.config import settings
from hailhq.core.languages import TTSProvider
from hailhq.core.providers.tts.assemblyai import AssemblyAISpeech
from hailhq.core.providers.tts.base import PCMAdapter
from hailhq.core.providers.tts.gemini import GeminiSpeech
from hailhq.core.providers.tts.xai import XaiSpeech

PCM_ADAPTERS: dict[str, type[PCMAdapter]] = {
    adapter.provider: adapter for adapter in (GeminiSpeech, XaiSpeech, AssemblyAISpeech)
}


def legacy_fallback_voice_id(
    provider: TTSProvider, voice_id: str | None, language: str | None
) -> str | None:
    """Keep a compatible per-call ID when a new TTS provider is skipped."""
    if not voice_id:
        return None
    if provider == "cartesia":
        try:
            return str(UUID(voice_id))
        except ValueError:
            return None
    if provider == "elevenlabs" and (
        voice_id == settings.elevenlabs_voice_id
        or any(
            voice.provider == provider and voice.voice_id == voice_id
            for voice in settings.voice_shortlist_preferences.get(language, [])
        )
    ):
        return voice_id
    return None
