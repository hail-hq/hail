"""TTS adapters used by the call worker and voice previews."""

from hailhq.core.config import settings
from hailhq.core.languages import TTSProvider
from hailhq.core.providers.tts.assemblyai import AssemblyAISpeech
from hailhq.core.providers.tts.base import PCMAdapter
from hailhq.core.providers.tts.gemini import GeminiSpeech
from hailhq.core.providers.tts.xai import XaiSpeech

PCM_ADAPTERS: dict[str, type[PCMAdapter]] = {
    adapter.provider: adapter for adapter in (GeminiSpeech, XaiSpeech, AssemblyAISpeech)
}


def configured_house_voice_provider(
    voice_id: str | None, language: str
) -> TTSProvider | None:
    """Identify a fallback voice using Hail's explicit provider configuration."""
    if not isinstance(voice_id, str) or not voice_id:
        return None
    providers: set[TTSProvider] = {
        choice.provider
        for choice in settings.voice_shortlist_preferences.get(language, [])
        if choice.voice_id == voice_id and choice.provider in ("cartesia", "elevenlabs")
    }
    if voice_id == settings.cartesia_voice_id:
        providers.add("cartesia")
    if voice_id == settings.elevenlabs_voice_id:
        providers.add("elevenlabs")
    return providers.pop() if len(providers) == 1 else None
