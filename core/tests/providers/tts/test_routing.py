from hailhq.core.config import settings
from hailhq.core.provider_config import ResolvedLayer
from hailhq.core.providers.tts.routing import resolve_tts_routes


def test_workspace_voice_and_fallback_share_one_ordered_resolution(monkeypatch):
    monkeypatch.setattr(settings, "cartesia_api_key", "house-cartesia")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
    monkeypatch.setattr(settings, "google_cloud_project", "house-project")
    org = ResolvedLayer(
        "gemini",
        "workspace-key",
        {"project": "customer-project", "voice_id": "Kore"},
        True,
    )

    primary, alternate, fallback = resolve_tts_routes(org, None, "en")

    assert (primary.provider, primary.voice_id, primary.uses_workspace_key) == (
        "gemini",
        "Kore",
        True,
    )
    assert primary.direct.project == "customer-project"
    assert alternate.provider == "cartesia"
    assert fallback.direct.project == "house-project"
    assert not fallback.uses_workspace_key


def test_explicit_workspace_voice_keeps_its_provider_model_and_key(monkeypatch):
    monkeypatch.setattr(settings, "hail_gemini_tts_enabled", True)
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
