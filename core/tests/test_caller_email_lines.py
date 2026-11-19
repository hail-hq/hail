"""Fixed lines of an email to the caller, in the call's language."""

import pytest
from hailhq.core.agent_tools.send_email import (
    MAX_ORG_NAME_CHARS,
    MAX_SUBJECT_CHARS,
    caller_email_body,
    caller_email_subject,
)
from hailhq.core.caller_email_lines import CALLER_EMAIL_LINES, caller_email_lines
from hailhq.core.languages import SUPPORTED_LANGUAGES


def test_every_supported_language_has_lines():
    assert CALLER_EMAIL_LINES.keys() == SUPPORTED_LANGUAGES.keys()


@pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
def test_language_has_every_line_with_the_org_slot(language):
    lines = CALLER_EMAIL_LINES[language]
    for named in (lines.subject, lines.first_line, lines.footer):
        assert named.count("{org}") == 1
    for unnamed in (lines.subject_unnamed, lines.first_line_unnamed):
        assert unnamed and "{" not in unnamed
    assert lines.footer_unnamed and "{" not in lines.footer_unnamed
    for line in lines:
        assert "\n" not in line and line == line.strip()
    if language != "en":
        assert lines != CALLER_EMAIL_LINES["en"]


@pytest.mark.parametrize("language", sorted(SUPPORTED_LANGUAGES))
def test_long_org_name_is_cut_and_the_fixed_words_stay(language):
    lines = CALLER_EMAIL_LINES[language]
    subject = caller_email_subject("S" * 200, "N" * 300, language)
    assert len(subject) <= MAX_SUBJECT_CHARS
    before, after = lines.subject.split("{org}")
    assert subject.startswith(before + "N")
    assert f"N{after}: S" in subject
    body = caller_email_body("Hi.", "N" * 300, language)
    assert "N" * MAX_ORG_NAME_CHARS in body
    assert "N" * (MAX_ORG_NAME_CHARS + 1) not in body


def test_italian_call_gets_italian_lines():
    assert caller_email_subject("Riepilogo", "Northgate Dental", "it") == (
        "Da una telefonata con Northgate Dental: Riepilogo"
    )
    body = caller_email_body("Ciao.", "Northgate Dental", "it")
    assert body.startswith(
        "Questa email è stata inviata in seguito a una telefonata con"
        " Northgate Dental.\n\nCiao.\n\n-- \n"
    )
    assert body.endswith("Se non ha fatto Lei questa richiesta, può ignorarla.")
    assert caller_email_subject("", None, "it") == "Da una telefonata"


def test_unknown_language_is_english():
    for language in (None, "xx", {"bad": "shape"}):
        assert caller_email_lines(language) == CALLER_EMAIL_LINES["en"]
        assert caller_email_subject("Hi", "Northgate Dental", language) == (
            "From a phone call with Northgate Dental: Hi"
        )


def test_joiners_stay_and_a_cut_never_splits_a_letter():
    # Marathi eyelash ra needs the zero width joiner.
    assert caller_email_subject("र\u094d\u200dया", None, "mr").endswith(
        ": र\u094d\u200dया"
    )
    # Bidi overrides still go.
    assert "\u202e" not in caller_email_subject("a\u202eb", None)
    prefix = caller_email_subject("", None, "hi")
    room = MAX_SUBJECT_CHARS - len(prefix) - 2
    # "कि" is a consonant and a vowel sign: the cut falls before the pair.
    subject = caller_email_subject("क" * (room - 1) + "कि", None, "hi")
    assert subject.endswith("क") and len(subject) == MAX_SUBJECT_CHARS - 1
