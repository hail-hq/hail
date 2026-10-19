from hailhq.core.voice_catalog import shortlist, voice_accent


def test_shortlist_keeps_default_and_offers_variety_in_the_requested_language():
    rows = [
        {"id": "british", "name": "George", "gender": "masculine", "language": "en"},
        {"id": "female", "name": "Narrator", "gender": "feminine", "language": "en"},
        {"id": "deep", "name": "Deep male", "gender": "masculine", "language": "en"},
        {"id": "french", "name": "French", "language": "fr"},
        {"id": "private", "name": "Private", "language": "en", "is_owner": True},
    ]
    voices = shortlist(rows, "en", "british")
    assert [v["id"] for v in voices] == ["british", "female", "deep"]
    assert [v["id"] for v in shortlist(rows, "fr")] == ["french"]
    assert shortlist(rows, "ar") == []


def test_shortlist_never_duplicates_a_default_or_exposes_an_unsafe_sample():
    row = {
        "id": "one",
        "name": "One",
        "language": "en",
        "preview_file_url": "javascript:bad",
    }
    voices = shortlist([row], "en", "one")
    assert len(voices) == 1
    assert voices[0]["preview_url"] is None


def test_accent_uses_native_locale_instead_of_voice_origin_or_foreign_accents():
    row = {
        "country": "US",
        "accents": [
            {"accent": "australian", "locale": "en-AU", "is_native": False},
            {"accent": "british", "locale": "en_GB", "is_native": True},
        ],
    }
    assert voice_accent(row, "en") == {"label": "British", "country_code": "GB"}
    assert voice_accent(row, "fr") is None
    assert voice_accent({"country": "GB", "language": "en"}, "en") is None


def test_accent_does_not_invent_a_flag_for_language_only_or_ambiguous_locales():
    row = {"accents": [{"accent": "english", "locale": "en", "is_native": True}]}
    assert voice_accent(row, "en") == {"label": "English", "country_code": None}
    row["accents"].append({"accent": "british", "locale": "en-GB", "is_native": True})
    assert voice_accent(row, "en") is None
