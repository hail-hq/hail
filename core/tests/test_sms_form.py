import pytest
from hailhq.core.providers.verification.sms_form import (
    campaign_text,
    field_problems,
    sms_requirements,
)


def test_business_form_lists_company_and_campaign_fields() -> None:
    req = sms_requirements("twilio", "business")
    names = [f.name for f in req.fields]
    assert names[:4] == ["legal_name", "business_type", "ein", "industry"]
    assert "campaign_description" in names and "sample_2" in names
    assert req.address_required is True
    assert req.documents == ()
    assert req.subject_types == ("person", "business")


def test_person_form_asks_for_mobile_not_ein() -> None:
    req = sms_requirements("telnyx", "person")
    names = [f.name for f in req.fields]
    assert "mobile_phone_number" in names and "ein" not in names
    mobile = next(f for f in req.fields if f.name == "mobile_phone_number")
    assert "not a Hail number" in mobile.help


def test_campaign_text_pads_short_values_to_carrier_minimums() -> None:
    text = campaign_text(
        {
            "campaign_description": "Order updates",
            "sample_1": "Hi",
            "sample_2": "Bye",
            "opt_in_description": "Web form",
            "has_links": "NO",
            "privacy_policy_url": "https://acme.example/privacy",
            "terms_url": "https://acme.example/terms",
        },
        "business",
    )
    assert len(text.description) >= 40
    assert len(text.message_flow) >= 40
    assert all(len(s) >= 20 for s in text.samples)
    assert text.use_case == "MIXED" and text.has_links is False


def test_form_asks_for_privacy_and_terms_links_for_both_subjects() -> None:
    for subject in ("person", "business"):
        fields = {f.name: f for f in sms_requirements("twilio", subject).fields}
        assert fields["privacy_policy_url"].kind == "url"
        assert fields["privacy_policy_url"].required is True
        assert fields["terms_url"].kind == "url"
        assert fields["terms_url"].required is True
        assert "not shared" in fields["privacy_policy_url"].help
        assert "unchecked" in fields["opt_in_description"].help


def test_message_flow_carries_links_and_consent_disclosures() -> None:
    text = campaign_text(
        {
            "campaign_description": "Order updates",
            "sample_1": "Hi",
            "sample_2": "Bye",
            "opt_in_description": "Customers tick an unchecked SMS box at checkout.",
            "has_links": "NO",
            "privacy_policy_url": "https://acme.example/privacy",
            "terms_url": "https://acme.example/terms",
        },
        "business",
    )
    flow = text.message_flow
    assert flow.startswith("Customers tick an unchecked SMS box at checkout.")
    for line in (
        "Consent is not a condition of purchase.",
        "Message frequency varies.",
        "Message and data rates may apply.",
        "Reply STOP to opt out, HELP for help.",
        "Privacy policy: https://acme.example/privacy",
        "Terms: https://acme.example/terms",
    ):
        assert line in flow
    assert text.privacy_policy_url == "https://acme.example/privacy"
    assert text.terms_url == "https://acme.example/terms"
    assert len(flow) <= 2048


def test_message_flow_never_exceeds_carrier_maximum() -> None:
    text = campaign_text(
        {
            "campaign_description": "x",
            "sample_1": "x",
            "sample_2": "x",
            "opt_in_description": "word " * 600,
            "has_links": "NO",
            "privacy_policy_url": "https://acme.example/privacy",
            "terms_url": "https://acme.example/terms",
        },
        "business",
    )
    assert len(text.message_flow) <= 2048
    assert "Privacy policy: https://acme.example/privacy" in text.message_flow


def test_person_use_case() -> None:
    assert (
        campaign_text(
            {
                "campaign_description": "x",
                "sample_1": "x",
                "sample_2": "x",
                "opt_in_description": "x",
                "has_links": "YES",
                "privacy_policy_url": "https://ada.example/privacy",
                "terms_url": "https://ada.example/terms",
            },
            "person",
        ).use_case
        == "SOLE_PROPRIETOR"
    )


@pytest.mark.parametrize(
    "field", ["sample_1", "campaign_description", "privacy_policy_url", "terms_url"]
)
def test_missing_required_field_raises(field) -> None:
    data = {
        "campaign_description": "x",
        "sample_1": "x",
        "sample_2": "x",
        "opt_in_description": "x",
        "has_links": "NO",
        "privacy_policy_url": "https://acme.example/privacy",
        "terms_url": "https://acme.example/terms",
    }
    data.pop(field)
    with pytest.raises(KeyError):
        campaign_text(data, "business")


GOOD = {
    "campaign_description": "x",
    "sample_1": "x",
    "sample_2": "x",
    "opt_in_description": "x",
    "has_links": "NO",
    "privacy_policy_url": "https://acme.example/privacy",
    "terms_url": "https://acme.example/terms",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "mobile_phone_number": "+15125550199",
}


def test_field_problems_accepts_a_complete_form() -> None:
    assert field_problems(GOOD, "person") == []


def test_field_problems_names_missing_required_fields() -> None:
    data = {**GOOD, "terms_url": " "}
    data.pop("sample_1")
    assert [(p.field, p.message) for p in field_problems(data, "person")] == [
        ("sample_1", "This is required."),
        ("terms_url", "This is required."),
    ]


@pytest.mark.parametrize(
    "value",
    ["acme.example/privacy", "ftp://acme.example/privacy", "https://", "n/a"],
)
def test_field_problems_rejects_a_link_without_scheme_or_host(value) -> None:
    problems = field_problems({**GOOD, "privacy_policy_url": value}, "person")
    assert [p.field for p in problems] == ["privacy_policy_url"]
    assert problems[0].message.startswith("Enter the full link")


def test_field_problems_rejects_a_link_over_the_cap() -> None:
    long = "https://acme.example/" + "a" * 500
    problems = field_problems({**GOOD, "terms_url": long}, "person")
    assert [(p.field, p.message) for p in problems] == [
        ("terms_url", "Use a shorter link.")
    ]


def test_message_flow_keeps_own_text_with_links_at_the_cap() -> None:
    link = "https://acme.example/" + "a" * 491
    text = campaign_text(
        {
            "campaign_description": "x",
            "sample_1": "x",
            "sample_2": "x",
            "opt_in_description": "own text " * 300,
            "has_links": "NO",
            "privacy_policy_url": link,
            "terms_url": link,
        },
        "business",
    )
    assert len(text.message_flow) <= 2048
    assert text.message_flow.startswith("own text own text")
    assert text.message_flow.endswith(f"Terms: {link}")
