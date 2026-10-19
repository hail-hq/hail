from hailhq.core.voice_catalog import shortlist


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
