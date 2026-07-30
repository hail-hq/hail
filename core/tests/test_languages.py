"""Tests for the language capability matrix and routing helpers."""

from __future__ import annotations

from typing import get_args

from hailhq.core.languages import (
    SUPPORTED_LANGUAGES,
    Language,
    default_stt_for,
    resolve_stt_provider,
    tts_providers_for,
    turn_mode_for,
)


def test_literal_matches_matrix_keys() -> None:
    assert set(get_args(Language)) == set(SUPPORTED_LANGUAGES)


def test_matrix_has_39_languages_and_excludes_mismatches() -> None:
    assert len(SUPPORTED_LANGUAGES) == 39
    for excluded in ("ka", "ml", "pa"):
        assert excluded not in SUPPORTED_LANGUAGES


def test_every_language_has_deepgram_and_cartesia() -> None:
    for caps in SUPPORTED_LANGUAGES.values():
        assert "deepgram" in caps.stt
        assert "cartesia" in caps.tts


def test_zh_excluded_from_speechmatics() -> None:
    """Speechmatics' Mandarin code is "cmn", which the LiveKit plugin's
    LanguageCode normalization rewrites back to "zh" — unreachable, so the
    matrix must not offer speechmatics for zh (a pinned zh call would fail
    at websocket start with invalid_language)."""
    assert "speechmatics" not in SUPPORTED_LANGUAGES["zh"].stt
    assert default_stt_for("zh") == "deepgram"


def test_default_stt_routing() -> None:
    assert default_stt_for(None) == "deepgram"  # English default
    assert default_stt_for("en") == "deepgram"  # semantic-turn language
    assert default_stt_for("de") == "deepgram"  # semantic-turn language
    assert default_stt_for("da") == "speechmatics"  # outside the 14
    assert default_stt_for("sv") == "speechmatics"
    assert default_stt_for("gu") == "deepgram"  # speechmatics can't do gu


def test_resolve_stt_provider_precedence() -> None:
    # org BYO row wins over auto
    assert resolve_stt_provider("speechmatics", "en") == "speechmatics"
    # no org row -> routed
    assert resolve_stt_provider(None, "da") == "speechmatics"
    assert resolve_stt_provider(None, None) == "deepgram"


def test_tts_providers_trim() -> None:
    assert tts_providers_for(None) == frozenset({"cartesia", "elevenlabs"})
    assert tts_providers_for("fr") == frozenset({"cartesia", "elevenlabs"})
    assert tts_providers_for("th") == frozenset({"cartesia"})  # no elevenlabs


def test_turn_mode_selection() -> None:
    assert turn_mode_for(None, "deepgram") == "semantic"
    assert turn_mode_for("en", "speechmatics") == "semantic"  # 14 beats stt
    assert turn_mode_for("da", "speechmatics") == "stt"
    assert turn_mode_for("da", "deepgram") == "vad"  # pinned away from sm
    assert turn_mode_for("gu", "deepgram") == "vad"  # nothing better exists
