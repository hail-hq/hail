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


def match_legacy_fallback_voice(
    voice_id: object, language: str | None
) -> tuple[TTSProvider | None, str | None]:
    """Infer a likely legacy provider without querying a catalogue mid-call."""
    if not isinstance(voice_id, str) or not voice_id:
        return None, None
    if (
        voice_id == settings.elevenlabs_voice_id
        or (len(voice_id) == 20 and voice_id.isascii() and voice_id.isalnum())
        or any(
            voice.provider == "elevenlabs" and voice.voice_id == voice_id
            for voice in settings.voice_shortlist_preferences.get(language, [])
        )
    ):
        return "elevenlabs", voice_id
    try:
        return "cartesia", str(UUID(voice_id))
    except ValueError:
        return None, None
