from hailhq.core.providers.verification.base import FieldSpec, Requirements
from hailhq.core.providers.verification.forms import text_length, validate_input


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
