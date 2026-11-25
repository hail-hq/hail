from hailhq.core.house_voices import (
    CARTESIA_LANGUAGE_VOICES,
    house_cartesia_voice,
    house_voice_language,
)
from hailhq.core.languages import SUPPORTED_LANGUAGES


def test_every_cartesia_language_but_english_has_a_voice() -> None:
    """A new supported language needs a voice made for it, or its calls
    speak with the English voice's accent."""
    cartesia = {
        code for code, caps in SUPPORTED_LANGUAGES.items() if "cartesia" in caps.tts
    }
    assert set(CARTESIA_LANGUAGE_VOICES) == cartesia - {"en"}


def test_each_voice_serves_one_language() -> None:
    voices = list(CARTESIA_LANGUAGE_VOICES.values())
    assert len(voices) == len(set(voices))
    for language, voice in CARTESIA_LANGUAGE_VOICES.items():
        assert house_voice_language(voice) == language


def test_english_and_unknown_languages_use_the_configured_voice() -> None:
    assert house_cartesia_voice("en") is None
    assert house_cartesia_voice(None) is None
    assert house_cartesia_voice({"unexpected": "shape"}) is None  # type: ignore[arg-type]
    assert house_cartesia_voice("de") == CARTESIA_LANGUAGE_VOICES["de"]
    assert house_voice_language("not-a-house-voice") is None
    assert house_voice_language(None) is None
