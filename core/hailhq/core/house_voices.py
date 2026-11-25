"""House default voice per call language.

A call with no chosen voice (none on the request, the saved agent or the
workspace) speaks with Hail's house Cartesia voice. One voice for every
language gives non-English calls a foreign accent, so each language has a
voice made for it. ``CARTESIA_VOICE_ID`` stays the voice for English and for
calls with no language.

These are defaults, not picker suggestions: docs/voice-shortlists.md covers
how the Call settings picker lists voices. Each entry is a public Cartesia
voice whose primary language and native accent match the key. Checked with
``GET /voices/{id}`` and one ``sonic-3.5`` synthesis each on 2026-11-25. A
test checks every supported language other than English has an entry.
"""

from __future__ import annotations

__all__ = ["CARTESIA_LANGUAGE_VOICES", "house_cartesia_voice", "house_voice_language"]

# Language code -> Cartesia voice id. One entry per supported language
# except English, which uses CARTESIA_VOICE_ID. Name and accent locale in
# the comment, for review.
CARTESIA_LANGUAGE_VOICES: dict[str, str] = {
    "ar": "b068aef8-2c18-4a1d-bafb-e8f6d2239d60",  # Khaled, ar-SA
    "bg": "3c90f2ab-ff91-434e-9e59-5ca2dcf6abc9",  # Teodora, bg-BG
    "bn": "9c35520a-73f9-4c9e-b6c6-69027a3819bd",  # Poulomi, bn-BD
    "cs": "82db1f84-5b96-4364-b04a-4c7ff80e2f8a",  # Jan, cs-CZ
    "da": "a466f9e2-28eb-4bb7-925c-8e8984950700",  # Søren, da-DK
    "de": "cd7b67f4-22a4-49a0-a197-3fa16f7e64d4",  # Alexander, de-DE
    "el": "f75d734d-31af-43e9-ba7c-e82b0697708e",  # Kostas, el-GR
    "es": "692279a6-ebcc-4735-a21e-9b8e31f6b3de",  # Gael, es-MX
    "fi": "7849a11e-4107-44ae-bc7e-77bea41ec019",  # Matti, fi-FI
    "fr": "5def377d-908b-4540-8bd7-3c968fcae351",  # Benoît, fr-FR
    "gu": "5ef8514d-954b-4f57-a8c8-c211c0dba93a",  # Niyati, gu-IN
    "he": "3a1546bd-6781-4376-984d-70b4e9f3d2c4",  # Dafna, he-IL
    "hi": "b3610b4c-0fd6-4912-89d1-ace6cb6054dd",  # Shailika, hi-IN
    "hr": "59694c02-a1df-46ec-8f52-6ebdc6b4a2c9",  # Domagoj, hr-HR
    "hu": "36e0c00b-1bfd-4ad7-a0e8-928d4cadca00",  # Gabor, hu-HU
    "id": "f3b7c445-d015-40f7-a665-9595e0bc0c90",  # Intan, id-ID
    "it": "c8403b5c-6465-4396-9065-a440d376528a",  # Alessio, it-IT
    "ja": "a935f15c-4b24-41dd-ba82-b90035720d16",  # Kaito, ja-JP
    "kn": "9f5d2c84-9f1a-4e95-8345-df2e245ff44c",  # Naveen, kn-IN
    "ko": "15628352-2ede-4f1b-89e6-ceda0c983fbc",  # Jiwoo, ko-KR
    "mr": "f227bc18-3704-47fe-b759-8c78a450fdfa",  # Suresh, mr-IN
    "ms": "2c6adcf6-1ff1-42c6-8162-26861f65853a",  # Farhan, ms-MY
    "nl": "95e9fdaf-cf0b-4739-b1de-3350ca50774a",  # Thijs, nl-NL
    "no": "4f7b1820-6263-4615-87a7-b105768d8f64",  # Kari, no-NO
    "pl": "43e52207-96fc-4e01-aaf8-cae317e43fdb",  # Kacper, pl-PL
    "pt": "074a0ce4-c88c-492f-8310-969d59f00e33",  # Gabriel, pt-BR
    "ro": "a98af725-3118-44e3-a7f6-6bd4fca20b32",  # Bogdan, ro-RO
    "ru": "069ff31a-5524-4945-a403-f746ee617507",  # Alexei, ru-RU
    "sk": "66f52369-ed35-4ee0-9cc8-1eceb2b0ed7d",  # Matúš, sk-SK
    "sv": "d3cc405c-a812-489b-afe5-f3d50a7a8310",  # August, sv-SE
    "ta": "19f28c21-ae34-499f-b64a-f7b09cd9b516",  # Karthik, ta-IN
    "te": "82c2afc8-ebbc-4802-8ccf-036dc0fa1e3b",  # Charan, te-IN
    "th": "4ff0f045-c140-4aa3-9210-529083f86fca",  # Supannee, th-TH
    "tl": "69d4de3c-ce06-4c67-a29f-cccf806ca0cd",  # Clarisse, tl-PH
    "tr": "c98c0a56-3371-4263-9aa5-abd5d5090c26",  # Kerem, tr-TR
    "uk": "bb4ddf2f-96e1-403b-976d-1361258545d0",  # Mykhailo, uk-UA
    "vi": "f1bdc97e-76d1-4bfe-95ba-f3a0bc7e6a7e",  # Trang, vi-VN
    "zh": "16212f18-4955-4be9-a6cd-2196ce2c11d1",  # Hao, zh-CN
}

_LANGUAGE_OF = {voice: language for language, voice in CARTESIA_LANGUAGE_VOICES.items()}


def house_cartesia_voice(language: str | None) -> str | None:
    """The house Cartesia voice made for ``language``, or None (English, no
    language, or no entry): the caller then uses ``CARTESIA_VOICE_ID``."""
    if not isinstance(language, str):
        return None
    return CARTESIA_LANGUAGE_VOICES.get(language)


def house_voice_language(voice_id: str | None) -> str | None:
    """The language a house table voice was made for, or None when
    ``voice_id`` is not in the table."""
    return _LANGUAGE_OF.get(voice_id) if voice_id else None
