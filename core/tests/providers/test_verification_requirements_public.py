"""The public requirements form leaves out the carrier; the stored hash does
not change."""

from hailhq.core.providers.verification.base import (
    DocumentOption,
    DocumentSlot,
    FieldSpec,
    Requirements,
    VerificationRequirements,
)


def _requirements() -> Requirements:
    return Requirements(
        provider="twilio",
        country_code="DE",
        number_type="local",
        subject_type="person",
        required=True,
        fields=(FieldSpec(name="first_name", label="First name"),),
        documents=(
            DocumentSlot(
                name="identity",
                label="Proof of identity",
                options=(DocumentOption(key="passport", label="Passport"),),
            ),
        ),
        address_required=True,
        subject_types=("person", "business"),
    )


def test_version_hash_is_unchanged_by_the_public_split() -> None:
    # Computed before VerificationRequirements existed. Stored rows hold this
    # value in carrier_verifications.requirements_version.
    assert _requirements().version == "c33c4e2df3907aed"


def test_public_requirements_have_no_provider() -> None:
    public = VerificationRequirements.model_validate(
        _requirements().model_dump(exclude={"provider"})
    )
    assert "provider" not in public.model_dump()
    assert "provider" not in VerificationRequirements.model_json_schema()["properties"]
