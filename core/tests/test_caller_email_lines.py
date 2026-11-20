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
    # The email list shows the phone call words before a long org name.
    assert not lines.subject.startswith("{org}")
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


def test_org_name_ending_in_a_virama_never_joins_the_fixed_word():
    # Telugu "bank" ends in a virama, and the te line puts a letter right
    # after {org}: a zero width non-joiner keeps them apart.
    bank = "\u0c2c\u0c4d\u0c2f\u0c3e\u0c02\u0c15\u0c4d"
    body = caller_email_body("Hi.", bank, "te")
    template = CALLER_EMAIL_LINES["te"].first_line
    assert body.startswith(template.replace("{org}", bank + "\u200c"))
    # Where a space or a dot follows {org}, the name is unchanged.
    assert caller_email_subject("Hi", bank) == f"From a phone call with {bank}: Hi"
    assert f"with {bank}." in caller_email_body("Hi.", bank)


def test_marks_with_no_letter_or_stacked_marks_go():
    assert caller_email_subject("\u0301" * 300, None) == "From a phone call"
    assert caller_email_subject("\u0336\u0336abc", None) == "From a phone call: abc"
    stacked = caller_email_subject("A" + "\u0336" * 80, None)
    assert stacked == "From a phone call: A" + "\u0336" * 4
    # Joiners between marks are dropped, and the marks still count as one run.
    joined = caller_email_subject("A" + "\u0336\u200d" * 80, None)
    assert joined == "From a phone call: A" + "\u0336" * 4
    assert caller_email_subject("Hi", "\u0336" * 5) == "From a phone call: Hi"


def test_org_name_drops_a_dangling_thai_leading_vowel():
    assert caller_email_subject("Hi", "Acme\u0e40", "th") == (
        "\u0e08\u0e32\u0e01\u0e01\u0e32\u0e23\u0e42\u0e17\u0e23\u0e28"
        "\u0e31\u0e1e\u0e17\u0e4c\u0e01\u0e31\u0e1a Acme: Hi"
    )
