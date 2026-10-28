import pytest
from hailhq.core.providers.verification.forms import length_problems, text_length
from hailhq.core.providers.verification.sms_form import (
    campaign_text,
    field_problems,
    sms_requirements,
)


def _too_long(data: dict, subject: str = "person") -> list[tuple[str, str]]:
    """The API's length check of this form (``forms.length_problems``)."""
    problems = length_problems(sms_requirements("telnyx", subject), data, {})
    return [(p.field, p.message) for p in problems]


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
            "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
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
            "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
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
            "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
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
                "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
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
        "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
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
    "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
    "privacy_policy_url": "https://acme.example/privacy",
    "terms_url": "https://acme.example/terms",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "mobile_phone_number": "+15125550199",
}


def test_field_problems_accepts_a_complete_form() -> None:
    assert field_problems("telnyx", GOOD, "person") == []


def test_field_problems_names_missing_required_fields() -> None:
    data = {**GOOD, "terms_url": " "}
    data.pop("sample_1")
    assert [(p.field, p.message) for p in field_problems("telnyx", data, "person")] == [
        ("sample_1", "This is required."),
        ("terms_url", "This is required."),
    ]


@pytest.mark.parametrize(
    "value",
    ["acme.example/privacy", "ftp://acme.example/privacy", "https://", "n/a"],
)
def test_field_problems_rejects_a_link_without_scheme_or_host(value) -> None:
    problems = field_problems("telnyx", {**GOOD, "privacy_policy_url": value}, "person")
    assert [p.field for p in problems] == ["privacy_policy_url"]
    assert problems[0].message.startswith("Enter the full link")


def test_a_link_over_the_cap_is_too_long() -> None:
    long = "https://acme.example/" + "a" * 500
    assert _too_long({**GOOD, "terms_url": long}) == [
        ("terms_url", "Use at most 512 characters.")
    ]
    assert field_problems("telnyx", {**GOOD, "terms_url": long}, "person") == []


def test_message_flow_keeps_own_text_with_links_under_the_cap() -> None:
    link = "https://acme.example/" + "a" * 491
    text = campaign_text(
        {
            "campaign_description": "x",
            "sample_1": "x",
            "sample_2": "x",
            "opt_in_description": "own text " * 95,
            "has_links": "NO",
            "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
            "privacy_policy_url": link,
            "terms_url": link,
        },
        "business",
    )
    assert len(text.message_flow) <= 2048
    assert text.message_flow.startswith("own text own text")
    assert text.message_flow.endswith(f"Terms: {link}")


def test_text_types_are_a_choice_of_one_to_five_for_a_person() -> None:
    for subject, least in (("person", 1), ("business", 2)):
        fields = {f.name: f for f in sms_requirements("telnyx", subject).fields}
        spec = fields["text_types"]
        assert spec.kind == "choices" and spec.required
        assert (spec.min_choices, spec.max_choices) == (least, 5)
        assert len(spec.options) == 10
        assert "CUSTOMER_CARE" in {o.key for o in spec.options}


@pytest.mark.parametrize(
    "value,message",
    [
        ("CUSTOMER_CARE,NOT_A_TYPE", "Pick from the list."),
        (
            "2FA,MARKETING,FRAUD_ALERT,SECURITY_ALERT,POLLING_VOTING,CUSTOMER_CARE",
            "Pick at most 5.",
        ),
    ],
)
def test_field_problems_checks_text_types(value, message) -> None:
    problems = field_problems("telnyx", {**GOOD, "text_types": value}, "person")
    assert [(p.field, p.message) for p in problems] == [("text_types", message)]


def test_a_company_picks_at_least_two_text_types() -> None:
    problems = field_problems(
        "telnyx", {**GOOD, "text_types": "CUSTOMER_CARE"}, "business"
    )
    assert ("text_types", "Pick at least 2.") in [
        (p.field, p.message) for p in problems
    ]


def test_campaign_text_keeps_the_picked_text_types_without_repeats() -> None:
    text = campaign_text({**GOOD, "text_types": " 2FA, MARKETING,2FA "}, "person")
    assert text.text_types == ["2FA", "MARKETING"]


def test_long_fields_carry_their_carrier_limits() -> None:
    fields = {f.name: f for f in sms_requirements("telnyx", "person").fields}
    assert fields["campaign_description"].max_length == 4096
    assert fields["sample_1"].max_length == fields["sample_2"].max_length == 1024
    assert fields["opt_in_description"].max_length == 861
    assert fields["first_name"].max_length is None


def test_the_opt_in_limit_leaves_room_for_the_fixed_text() -> None:
    # The longest links and the consent lines still fit in the carrier's 2048.
    long = "https://acme.example/" + "a" * (512 - len("https://acme.example/"))
    data = {
        **GOOD,
        "opt_in_description": "x" * 861,
        "privacy_policy_url": long,
        "terms_url": long,
    }
    assert field_problems("telnyx", data, "person") == []
    assert _too_long(data) == []
    text = campaign_text(data, "person")
    # Nothing is cut: the opt-in text and both links fill the 2048 exactly.
    assert text.message_flow.startswith("x" * 861)
    assert len(text.message_flow) == 2048


def test_text_over_the_limit_is_too_long() -> None:
    assert _too_long({**GOOD, "sample_1": "s" * 1025}) == [
        ("sample_1", "Use at most 1024 characters.")
    ]


def _with_links(opt_in: str) -> dict:
    link = "https://acme.example/" + "a" * 491
    return {
        **GOOD,
        "opt_in_description": opt_in,
        "privacy_policy_url": link,
        "terms_url": link,
    }


def test_an_unchecked_opt_in_text_is_cut_to_the_carrier_limit() -> None:
    # A caller that skipped the length check: the opt-in text is cut so the
    # whole flow fits the carrier's 2048, and the fixed text stays whole.
    data = _with_links("own text " * 300)
    assert _too_long(data) == [("opt_in_description", "Use at most 861 characters.")]
    text = campaign_text(data, "business")
    assert text_length(text.message_flow) <= 2048
    assert text.message_flow.startswith("own text own text")
    assert text.message_flow.endswith(f"Terms: {data['terms_url']}")


def test_the_opt_in_cut_counts_utf16_units_and_keeps_emoji_whole() -> None:
    # 1100 emoji are 2200 UTF-16 units: counted by code points they would
    # look short enough, and the flow would pass the carrier's 2048.
    data = _with_links("\U0001f600" * 1100)
    text = campaign_text(data, "business")
    assert text_length(text.message_flow) <= 2048
    own = text.message_flow.split(" Consent is not")[0]
    assert own and set(own) == {"\U0001f600"}  # no half emoji
    assert text.message_flow.endswith(f"Terms: {data['terms_url']}")


def test_only_the_telnyx_form_asks_for_text_types() -> None:
    for subject in ("person", "business"):
        twilio = [f.name for f in sms_requirements("twilio", subject).fields]
        telnyx = [f.name for f in sms_requirements("telnyx", subject).fields]
        assert "text_types" not in twilio and "text_types" in telnyx
        assert [n for n in telnyx if n != "text_types"] == twilio


def test_a_twilio_form_without_text_types_passes() -> None:
    data = {k: v for k, v in GOOD.items() if k != "text_types"}
    assert field_problems("twilio", data, "person") == []
    assert [p.field for p in field_problems("telnyx", data, "person")] == ["text_types"]


def test_campaign_text_keeps_the_contact_email() -> None:
    person = campaign_text({**GOOD, "email": " ada@example.com "}, "person")
    assert person.contact_email == "ada@example.com"
    company = campaign_text({**GOOD, "rep_email": "ops@acme.example"}, "business")
    assert company.contact_email == "ops@acme.example"


def test_length_counts_characters_as_the_carrier_does() -> None:
    # An emoji is 2 UTF-16 code units, as the carrier and the browser count.
    assert _too_long({**GOOD, "sample_1": "s" * 1022 + "\U0001f600"}) == []
    assert _too_long({**GOOD, "sample_1": "s" * 1023 + "\U0001f600"}) == [
        ("sample_1", "Use at most 1024 characters.")
    ]


def test_links_carry_their_limit() -> None:
    for subject in ("person", "business"):
        fields = sms_requirements("telnyx", subject).fields
        assert {f.name: f.max_length for f in fields if f.kind == "url"} == {
            **({"website": 512} if subject == "business" else {}),
            "privacy_policy_url": 512,
            "terms_url": 512,
        }
