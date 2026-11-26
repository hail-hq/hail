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


def legacy_fallback_voice_provider(voice_id: object) -> TTSProvider | None:
    """Infer a likely legacy provider without querying a catalogue mid-call."""
    if not isinstance(voice_id, str) or not voice_id:
        return None
    if voice_id == settings.elevenlabs_voice_id or (
        len(voice_id) == 20 and voice_id.isascii() and voice_id.isalnum()
    ):
        return "elevenlabs"
    try:
        UUID(voice_id)
    except ValueError:
        return None
    return "cartesia"
