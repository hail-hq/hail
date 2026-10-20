from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from hailhq.core import voice_catalog as catalog
from hailhq.core.config import VoicePreference, settings


@pytest.fixture(autouse=True)
def catalogue_settings(monkeypatch):
    catalog._cache.clear()
    monkeypatch.setattr(settings, "cartesia_api_key", "cart-key")
    monkeypatch.setattr(settings, "cartesia_model", "sonic-3.5")
    monkeypatch.setattr(settings, "cartesia_voice_id", "current")
    monkeypatch.setattr(settings, "eleven_api_key", "")
    monkeypatch.setattr(settings, "elevenlabs_model", "test-model")
    monkeypatch.setattr(settings, "elevenlabs_voice_id", "")
    monkeypatch.setattr(settings, "voice_shortlist_preferences", {})
    monkeypatch.setattr(settings, "cartesia_model_languages", {})


def cart_row(id="current", language="en", **extra):
    return {
        "id": id,
        "name": id.title(),
        "language": language,
        "access": "public",
        "status": "active",
        "gender": "masculine",
        **extra,
    }


def cart_choices(rows, language="en"):
    return catalog.cartesia_choices(rows, language, settings.cartesia_model)


def test_shortlist_preserves_configured_default_and_metadata_variety():
    rows = [
        cart_row(),
        cart_row("woman", gender="feminine"),
        cart_row("other"),
        cart_row("french", "fr"),
        cart_row("private", is_owner=True),
    ]
    voices = catalog.shortlist(cart_choices(rows), "en")
    assert [v["id"] for v in voices] == ["current", "woman", "other"]
    assert [v["id"] for v in catalog.shortlist(cart_choices(rows, "fr"), "fr")] == [
        "french"
    ]


def test_preferences_cannot_bypass_language_eligibility(monkeypatch):
    monkeypatch.setattr(
        settings,
        "voice_shortlist_preferences",
        {
            "fr": [
                VoicePreference(
                    provider="cartesia", voice_id="current", reason="British default"
                ),
                VoicePreference(
                    provider="cartesia",
                    voice_id="french",
                    reason="French listening review",
                ),
            ]
        },
    )
    voices = catalog.shortlist(
        cart_choices([cart_row(), cart_row("french", "fr")], "fr"), "fr"
    )
    assert [v["id"] for v in voices] == ["french"]


def test_description_keywords_do_not_determine_selection():
    rows = [
        cart_row("a"),
        cart_row("b"),
        cart_row("c"),
        cart_row("z", description="deep calm conversational warm narrator"),
    ]
    assert [v["id"] for v in catalog.shortlist(cart_choices(rows), "en")] == [
        "a",
        "b",
        "c",
    ]


def test_multilingual_accents_confirm_support_without_claiming_native_origin():
    row = cart_row(
        accents=[
            {"accent": "british", "locale": "en-GB", "is_native": True},
            {"accent": "parisian", "locale": "fr_FR", "is_native": False},
        ]
    )
    assert cart_choices([row], "fr")[0]["accent"] == {
        "label": "Parisian",
        "country_code": "FR",
    }
    assert cart_choices([row], "de") == []
    assert catalog.voice_accent(row, "en") == {"label": "British", "country_code": "GB"}


def test_accent_does_not_guess_from_origin_language_or_ambiguous_locales():
    assert catalog.voice_accent({"country": "GB", "language": "en"}, "en") is None
    row = {"accents": [{"accent": "english", "locale": "en", "is_native": True}]}
    assert catalog.voice_accent(row, "en") == {"label": "English", "country_code": None}
    row["accents"].append({"accent": "british", "locale": "en-GB", "is_native": False})
    assert catalog.voice_accent(row, "en") is None


def test_unknown_model_requires_explicit_capability_configuration(monkeypatch):
    assert catalog.cartesia_choices([cart_row()], "en", "custom-model") == []
    monkeypatch.setattr(settings, "cartesia_model_languages", {"custom-model": ["en"]})
    assert len(catalog.cartesia_choices([cart_row()], "en", "custom-model")) == 1
    assert (
        catalog.cartesia_choices([cart_row("french", "fr")], "fr", "custom-model") == []
    )


def test_elevenlabs_requires_both_model_and_voice_language_evidence():
    models = [
        {
            "model_id": "model-a",
            "can_do_text_to_speech": True,
            "languages": [{"language_id": "fr"}],
        }
    ]
    rows = [
        {
            "voice_id": "one",
            "name": "One",
            "category": "premade",
            "labels": {"language": "en"},
            "verified_languages": [
                {
                    "language": "fr",
                    "model_id": "model-a",
                    "accent": "standard",
                    "locale": "fr-FR",
                }
            ],
        }
    ]
    assert len(catalog.elevenlabs_choices(rows, models, "fr", "model-a")) == 1
    assert catalog.elevenlabs_choices(rows, models, "en", "model-a") == []
    assert catalog.elevenlabs_choices(rows, models, "fr", "model-b") == []
    rows[0]["verified_languages"][0]["model_id"] = "different"
    assert catalog.elevenlabs_choices(rows, models, "fr", "model-a") == []


def test_identity_includes_provider_and_private_clones_are_not_recommended():
    choices = cart_choices([cart_row("same")])
    choices.append({**choices[0], "provider": "elevenlabs"})
    assert len(catalog.shortlist(choices, "en")) == 2
    assert (
        catalog.cartesia_choices([cart_row(access="private")], "en", "sonic-3.5") == []
    )
    assert (
        catalog.elevenlabs_choices([{"category": "cloned"}], [], "en", "test-model")
        == []
    )


@pytest.mark.parametrize(
    "provider,items,cursor,next_field",
    [
        ("cartesia", "data", "starting_after", "next_page"),
        ("elevenlabs", "voices", "next_page_token", "next_page_token"),
    ],
)
async def test_catalogue_reads_later_pages_and_rejects_a_looping_cursor(
    provider, items, cursor, next_field
):
    url = (
        "https://api.cartesia.ai/voices"
        if provider == "cartesia"
        else "https://api.elevenlabs.io/v2/voices"
    )
    with respx.mock as mock:
        route = mock.get(url).mock(
            side_effect=[
                httpx.Response(
                    200,
                    json={
                        items: [{"id": "page-one"}],
                        "has_more": True,
                        next_field: "next",
                    },
                ),
                httpx.Response(
                    200, json={items: [{"id": "page-two"}], "has_more": False}
                ),
            ]
        )
        async with httpx.AsyncClient() as client:
            assert [v["id"] for v in await catalog._pages(client, provider)] == [
                "page-one",
                "page-two",
            ]
            assert route.calls[1].request.url.params[cursor] == "next"
        route.mock(
            return_value=httpx.Response(
                200, json={items: [], "has_more": True, next_field: "loop"}
            )
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="did not advance"):
                await catalog._pages(client, provider)


async def test_missing_cursor_never_silently_returns_page_one():
    with respx.mock:
        respx.get("https://api.cartesia.ai/voices").mock(
            return_value=httpx.Response(200, json={"data": [], "has_more": True})
        )
        async with httpx.AsyncClient() as client:
            with pytest.raises(ValueError, match="did not advance"):
                await catalog._pages(client, "cartesia")


async def test_failed_provider_keeps_other_choices_and_pipeline_languages(monkeypatch):
    monkeypatch.setattr(settings, "eleven_api_key", "eleven-key")
    monkeypatch.setattr(
        catalog,
        "_provider_catalogue",
        AsyncMock(
            side_effect=[
                {"rows": [cart_row()], "models": []},
                httpx.ConnectError("offline"),
            ]
        ),
    )
    result = await catalog.voice_catalogue("en")
    assert [v["id"] for v in result["voices"]] == ["current"]
    assert result["partial"] is True
    assert {v["code"] for v in result["languages"]} == set(catalog.SUPPORTED_LANGUAGES)


async def test_catalogue_cache_is_reused_and_invalidated_by_model(monkeypatch):
    with respx.mock as mock:
        route = mock.get("https://api.cartesia.ai/voices").mock(
            return_value=httpx.Response(
                200, json={"data": [cart_row()], "has_more": False}
            )
        )
        await catalog.voice_catalogue("en")
        await catalog.voice_catalogue("fr")
        assert route.call_count == 1
        monkeypatch.setattr(settings, "cartesia_model", "sonic-3.6")
        await catalog.voice_catalogue("en")
        assert route.call_count == 2
        assert all(call.request.method == "GET" for call in mock.calls)


async def test_retained_voice_validation_checks_supported_language_without_synthesis():
    with respx.mock as mock:
        mock.get("https://api.cartesia.ai/voices/custom").mock(
            return_value=httpx.Response(200, json=cart_row("custom", access="private"))
        )
        await catalog.validate_voice_language(
            "cartesia", "custom", "sonic-3.5", "customer-key", "en"
        )
        with pytest.raises(ValueError, match="could not be verified"):
            await catalog.validate_voice_language(
                "cartesia", "custom", "sonic-3.5", "customer-key", "fr"
            )
        assert all(call.request.method == "GET" for call in mock.calls)


def test_elevenlabs_tagalog_uses_the_filipino_provider_code():
    models = [
        {
            "model_id": "model-a",
            "can_do_text_to_speech": True,
            "languages": [{"language_id": "fil"}],
        }
    ]
    rows = [
        {
            "voice_id": "one",
            "name": "One",
            "category": "premade",
            "verified_languages": [
                {"language": "fil", "model_id": "model-a", "locale": "fil-PH"}
            ],
        }
    ]
    choices = catalog.elevenlabs_choices(rows, models, "tl", "model-a")
    assert [c["language"] for c in choices] == ["tl"]
