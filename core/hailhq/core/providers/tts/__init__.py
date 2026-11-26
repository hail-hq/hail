"""TTS adapters used by the call worker and voice previews."""

from hailhq.core.providers.tts.assemblyai import AssemblyAISpeech
from hailhq.core.providers.tts.base import PCMAdapter
from hailhq.core.providers.tts.gemini import GeminiSpeech
from hailhq.core.providers.tts.xai import XaiSpeech

PCM_ADAPTERS: dict[str, type[PCMAdapter]] = {
    adapter.provider: adapter for adapter in (GeminiSpeech, XaiSpeech, AssemblyAISpeech)
}
