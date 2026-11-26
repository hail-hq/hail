from hailhq.core.config import settings
from hailhq.core.provider_config import ResolvedLayer
from hailhq.core.voice_routing import resolve_tts_routes


def test_workspace_voice_and_fallback_share_one_ordered_resolution(monkeypatch):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setattr(settings, "google_cloud_project", "house-project")
    org = ResolvedLayer(
        "gemini",
        "workspace-key",
        {"voice_id": "Kore"},
        True,
    )

    primary, alternate, fallback = resolve_tts_routes(org, None, "en")

    assert (primary.provider, primary.voice_id, primary.uses_workspace_key) == (
        "gemini",
        "Kore",
        True,
    )
    assert primary.api_key == "workspace-key"
    assert alternate.provider == "cartesia"
    assert fallback.provider == "gemini"
    assert fallback.api_key is None
    assert not fallback.uses_workspace_key


def test_explicit_workspace_voice_keeps_its_provider_model_and_key(monkeypatch):
    monkeypatch.setattr(settings, "google_cloud_project", "house-project")
    org = ResolvedLayer(
        "elevenlabs",
        "workspace-key",
        {"model": "saved-model", "voice_id": "saved-voice"},
        False,
    )

    (voice,) = resolve_tts_routes(org, None, "en")

    assert (voice.provider, voice.model, voice.voice_id) == (
        "elevenlabs",
        "saved-model",
        "saved-voice",
    )
    assert voice.api_key == "workspace-key"


def test_house_elevenlabs_resolves_the_builtin_voice_when_unset(monkeypatch):
    monkeypatch.setattr(settings, "cartesia_api_key", "")
    monkeypatch.setattr(settings, "eleven_api_key", "house-eleven")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "")
    monkeypatch.setattr(settings, "google_cloud_project", "")
    monkeypatch.setattr(settings, "google_api_key", "")
    (voice,) = resolve_tts_routes(None, None, "en")
    assert voice.provider == "elevenlabs"
    assert voice.voice_id == "hpp4J3VqNfWAUOO0d1Us"


def test_workspace_cartesia_empty_key_uses_the_same_house_provider(monkeypatch):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    org = ResolvedLayer("cartesia", "", {"voice_id": "saved-voice"}, False)

    (voice,) = resolve_tts_routes(org, None, "fr")

    assert voice.provider == "cartesia"
    assert voice.voice_id == "saved-voice"
    assert voice.api_key == "house-cartesia"
    assert not voice.uses_workspace_key
