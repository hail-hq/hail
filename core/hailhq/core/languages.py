"""Language capability matrix for the voice pipeline.

Single source of truth for which call languages Hail supports and which
STT/TTS provider serves each one. Derived from official provider docs on
2026-07-29 (research spec:
``docs/superpowers/specs/2026-07-29-multi-language-voicebot-design.md``):

* Deepgram nova-3 streaming languages:
  https://developers.deepgram.com/docs/models-languages-overview
* Speechmatics real-time languages:
  https://docs.speechmatics.com/speech-to-text/languages
* Cartesia sonic-3.5 languages:
  https://docs.cartesia.ai/build-with-cartesia/tts-models/latest
* ElevenLabs eleven_turbo_v2_5 languages:
  https://elevenlabs.io/docs/models
* LiveKit audio TurnDetector languages:
  https://docs.livekit.io/agents/build/turns/turn-detector/

Supported = (nova-3 ∪ Speechmatics STT) ∩ sonic-3.5. Excluded for
capability mismatch (STT exists only on Whisper-based models Hail doesn't
run): ka, ml, pa.
"""

from __future__ import annotations

import dataclasses
from typing import Literal

__all__ = [
    "SUPPORTED_LANGUAGES",
    "Language",
    "LanguageCaps",
    "default_stt_for",
    "elevenlabs_language",
    "resolve_stt_provider",
    "tts_providers_for",
    "turn_mode_for",
]

# Keep in sync with SUPPORTED_LANGUAGES. test_literal_matches_matrix_keys
# guards the pairing. A Literal (not a StrEnum) so pydantic renders a plain
# string enum in the OpenAPI spec.
Language = Literal[
    "ar",
    "bg",
    "bn",
    "cs",
    "da",
    "de",
    "el",
    "en",
    "es",
    "fi",
    "fr",
    "gu",
    "he",
    "hi",
    "hr",
    "hu",
    "id",
    "it",
    "ja",
    "kn",
    "ko",
    "mr",
    "ms",
    "nl",
    "no",
    "pl",
    "pt",
    "ro",
    "ru",
    "sk",
    "sv",
    "ta",
    "te",
    "th",
    "tl",
    "tr",
    "uk",
    "vi",
    "zh",
]

_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "cs": "Czech",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "es": "Spanish",
    "fi": "Finnish",
    "fr": "French",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "ko": "Korean",
    "mr": "Marathi",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sv": "Swedish",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "vi": "Vietnamese",
    "zh": "Chinese",
}

# LiveKit audio TurnDetector coverage (2026-10-18).
# Arabic is supported; Russian retains STT/VAD turn handling.
# https://docs.livekit.io/agents/logic/turns/turn-detector/
_SEMANTIC_TURN = frozenset(
    {"en", "es", "fr", "de", "it", "pt", "nl", "zh", "ja", "ko", "id", "tr", "ar", "hi"}
)
# Languages Speechmatics real-time STT does NOT cover (nova-3 only).
# "zh" is here not because Speechmatics lacks Mandarin but because its code
# for it is "cmn", and the LiveKit plugin's LanguageCode normalization maps
# "cmn" back to "zh" (livekit.agents language.py), so the code Speechmatics
# accepts can never reach the wire: a speechmatics-routed zh call fails at
# websocket start with invalid_language.
_NO_SPEECHMATICS = frozenset({"gu", "kn", "te", "zh"})
# Languages ElevenLabs eleven_turbo_v2_5 does NOT cover (Cartesia only).
_NO_ELEVENLABS = frozenset({"bn", "gu", "he", "kn", "te", "th", "mr"})


@dataclasses.dataclass(frozen=True)
class LanguageCaps:
    name: str
    stt: frozenset[str]
    tts: frozenset[str]
    semantic_turn: bool


SUPPORTED_LANGUAGES: dict[str, LanguageCaps] = {
    code: LanguageCaps(
        name=_NAMES[code],
        stt=(
            frozenset({"deepgram"})
            if code in _NO_SPEECHMATICS
            else frozenset({"deepgram", "speechmatics"})
        ),
        tts=(
            frozenset({"cartesia"})
            if code in _NO_ELEVENLABS
            else frozenset({"cartesia", "elevenlabs"})
        ),
        semantic_turn=code in _SEMANTIC_TURN,
    )
    for code in _NAMES
}

# Cartesia does not expose model language capabilities through its catalogue.
# Conservative snapshots: only languages also supported by Hail's STT pipeline.
# Reviewed 2026-10-19 against the official Sonic 3.5 / Sonic 3.6 model pages:
# https://docs.cartesia.ai/build-with-cartesia/tts-models/sonic-3-5
# https://docs.cartesia.ai/build-with-cartesia/tts-models/latest
# Unknown models require explicit operator capability configuration. This does
# not change which models an advanced BYO configuration can use.
CARTESIA_MODEL_LANGUAGES: dict[str, frozenset[str]] = dict.fromkeys(
    ("sonic-3.5", "sonic-3.5-2026-05-04", "sonic-3.6", "sonic-3.6-2026-08-27"),
    frozenset(SUPPORTED_LANGUAGES),
)


# ElevenLabs uses ISO 639-3 where a language has no ISO 639-1 code.
# https://elevenlabs.io/docs/models lists Filipino as "fil".
_ELEVENLABS_CODES = {"tl": "fil"}


def elevenlabs_language(language: str) -> str:
    """Hail's language code as ElevenLabs models, voices and requests spell it."""
    return _ELEVENLABS_CODES.get(language, language)


def default_stt_for(language: str | None) -> str:
    """Auto-route: Deepgram wherever the semantic turn detector works
    (or no language is set), Speechmatics where it doesn't and Speechmatics
    covers the language, Deepgram otherwise."""
    if language is None:
        return "deepgram"
    caps = SUPPORTED_LANGUAGES[language]
    if caps.semantic_turn:
        return "deepgram"
    if "speechmatics" in caps.stt:
        return "speechmatics"
    return "deepgram"


def resolve_stt_provider(org_provider: str | None, language: str | None) -> str:
    """Precedence: org BYO standing choice > language auto-routing.

    STT provider selection is console-BYO-only. There is no per-call
    override. An org row (set via the console) always wins; absent one,
    the call auto-routes by language (see :func:`default_stt_for`).
    """
    if org_provider is not None:
        return org_provider
    return default_stt_for(language)


def tts_providers_for(language: str | None) -> frozenset[str]:
    if language is None:
        return frozenset({"cartesia", "elevenlabs"})
    return SUPPORTED_LANGUAGES[language].tts


def turn_mode_for(language: str | None, stt_provider: str) -> str:
    """Pick the turn-detection strategy for one call.

    "semantic": LiveKit audio TurnDetector (14 languages, any STT).
    "stt": Speechmatics drives end-of-turn from the transcript stream.
    "vad": silence-gap only; the floor when nothing better exists.
    """
    if language is None or SUPPORTED_LANGUAGES[language].semantic_turn:
        return "semantic"
    if stt_provider == "speechmatics":
        return "stt"
    return "vad"
