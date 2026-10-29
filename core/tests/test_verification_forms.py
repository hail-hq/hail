from hailhq.core.providers.verification.base import (
    DocumentInput,
    DocumentOption,
    DocumentSlot,
    FieldSpec,
    Requirements,
)
from hailhq.core.providers.verification.forms import (
    check_field,
    cut_text,
    length_problems,
    text_length,
    validate_input,
)


def _requirements(*fields: FieldSpec) -> Requirements:
    return Requirements(
        provider="telnyx",
        country_code="SE",
        number_type="local",
        subject_type="business",
        required=True,
        fields=fields,
        documents=(),
        address_required=False,
    )


def test_text_length_counts_utf16_code_units() -> None:
    assert text_length("abc") == 3
    assert text_length("é") == 1
    assert text_length("\U0001f600") == 2


def test_validate_input_refuses_a_value_over_max_length() -> None:
    req = _requirements(FieldSpec(name="email", label="Email", max_length=10))
    assert validate_input(req, {"email": "a" * 10}, None, {}) == []
    problems = validate_input(req, {"email": "a" * 11}, None, {})
    assert [(p.field, p.message) for p in problems] == [
        ("email", "Use at most 10 characters.")
    ]
    # Counted as the carrier counts: an emoji is 2.
    problems = validate_input(req, {"email": "a" * 9 + "\U0001f600"}, None, {})
    assert [p.message for p in problems] == ["Use at most 10 characters."]


def test_validate_input_ignores_padding_and_fields_without_a_limit() -> None:
    req = _requirements(
        FieldSpec(name="email", label="Email", max_length=5),
        FieldSpec(name="note", label="Note"),
    )
    fields = {"email": "  abcde  ", "note": "x" * 5000}
    assert validate_input(req, fields, None, {}) == []


def test_check_field_reports_length_and_pattern_together() -> None:
    spec = FieldSpec(name="code", label="Code", pattern=r"^\d+$", max_length=3)
    problems = check_field(spec, "abcd", "code")
    assert [p.message for p in problems] == [
        "Use at most 3 characters.",
        "Code is not valid.",
    ]
    assert check_field(spec, "123", "code") == []


def test_cut_text_counts_utf16_units_and_never_splits_an_emoji() -> None:
    assert cut_text("abc", 5) == "abc"
    assert cut_text("abc", 2) == "ab"
    assert cut_text("a\U0001f600b", 2) == "a"
    assert cut_text("a\U0001f600b", 3) == "a\U0001f600"
    assert cut_text("abc", 0) == ""


def test_length_problems_checks_fields_and_the_chosen_document_type() -> None:
    slot = DocumentSlot(
        name="id",
        label="ID",
        options=(
            DocumentOption(
                key="passport",
                label="Passport",
                fields=(FieldSpec(name="number", label="Number", max_length=4),),
            ),
        ),
    )
    req = Requirements(
        provider="didww",
        country_code="DE",
        number_type="local",
        subject_type="person",
        required=True,
        # No pattern, required or other rule here: only lengths.
        fields=(
            FieldSpec(name="name", label="Name", max_length=3),
            FieldSpec(name="note", label="Note", pattern=r"^x$"),
        ),
        documents=(slot,),
        address_required=False,
    )
    docs = {"id": DocumentInput(option="passport", fields={"number": "12345"})}
    problems = length_problems(req, {"name": "abcd", "note": "nope"}, docs)
    assert [(p.field, p.message) for p in problems] == [
        ("name", "Use at most 3 characters."),
        ("id.number", "Use at most 4 characters."),
    ]
    assert length_problems(req, {}, {}) == []
