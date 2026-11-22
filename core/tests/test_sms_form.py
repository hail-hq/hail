from dataclasses import asdict

import pytest
from hailhq.core.providers.verification import telnyx_sms, twilio_sms
from hailhq.core.providers.verification.forms import length_problems, text_length
from hailhq.core.providers.verification.sms_form import (
    campaign_text,
    field_problems,
    sms_requirements,
    tax_number,
)
from hailhq.core.providers.verification.telnyx_sms import TelnyxSmsRegistrationProvider
from hailhq.core.providers.verification.twilio_sms import TwilioSmsRegistrationProvider

TELNYX = TelnyxSmsRegistrationProvider(api_key="k")
TWILIO = TwilioSmsRegistrationProvider(
    account_sid="AC1",
    auth_token="t",
    client=object(),
    primary_profile_sid="BU1",
    sms_provider=object(),
)

GOOD = {
    "sender_country": "US",
    "has_tax_id": "NO",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@acme.org",
    "mobile_phone_number": "+15125550199",
    "program_name": "Acme",
    "opt_in_message": "Acme: Enrolled for appointment updates, once per appointment. Msg and data rates may apply. Reply HELP for help, STOP to stop.",
    "campaign_description": "Acme sends appointment reminders and replies to customer support questions.",
    "message_frequency": "once per appointment",
    "disclosure_purposes_confirmed": "YES",
    "text_types": "CUSTOMER_CARE",
    "sample_1": "Acme: Your appointment is tomorrow at 2pm. Reply STOP to opt out.",
    "sample_1_type": "CUSTOMER_CARE",
    "sample_2": "Acme: We can reschedule your appointment to Friday. Reply STOP to opt out.",
    "sample_2_type": "CUSTOMER_CARE",
    "consent_methods": "WEBSITE",
    "opt_in_description": "Customers visit our booking page and select the separate optional SMS reminder checkbox before submitting.",
    "consent_disclosure": "Get Acme appointment reminders, once per appointment. Message and data rates may apply. Reply STOP to opt out or HELP for help. https://acme.org/privacy https://acme.org/terms",
    "consent_records": "We privately retain the checkbox selection, timestamp and disclosure version.",
    "consent_optional": "YES",
    "marketing_consent": "NO",
    "evidence_redacted": "YES",
    "website_url": "https://acme.org/book",
    "website_evidence_url": "https://acme.org/evidence/booking.png",
    "website_action": "Select the initially unchecked SMS reminder checkbox, then submit.",
    "privacy_policy_url": "https://acme.org/privacy",
    "terms_url": "https://acme.org/terms",
    "policies_confirmed": "YES",
    "has_links": "NO",
    "has_phone": "NO",
    "age_gated": "NO",
    "direct_lending": "NO",
    "restricted_content": "NONE",
}
BUSINESS = {
    **GOOD,
    "has_tax_id": "YES",
    "registration_country": "US",
    "legal_name": "Acme Ltd",
    "business_type": "PRIVATE_PROFIT",
    "ein": "12-3456789",
    "industry": "PROFESSIONAL",
    "website": "https://acme.org",
    "rep_name": "Ada Lovelace",
    "rep_email": "ada@acme.org",
    "rep_phone": "+15125550199",
    "rep_title": "Owner",
    "daily_texts": "OVER_2000",
}


def errors(data, subject="person", provider=TELNYX):
    return [(p.field, p.message) for p in field_problems(provider, data, subject)]


def error_fields(data, subject="person", provider=TELNYX):
    return {field for field, _ in errors(data, subject, provider)}


@pytest.mark.parametrize("provider", [TELNYX, TWILIO])
@pytest.mark.parametrize("subject,data", [("person", GOOD), ("business", BUSINESS)])
def test_complete_form_passes(provider, subject, data):
    assert errors(data, subject, provider) == []
    assert length_problems(sms_requirements(provider, subject), data, {}) == []


def test_form_supports_eligibility_policies_and_all_purposes_on_both_providers():
    for provider in (TELNYX, TWILIO):
        for subject in ("person", "business"):
            req = sms_requirements(provider, subject)
            specs = {f.name: f for f in req.fields}
            assert len(specs) == len(req.fields)
            assert specs["sender_country"].required and specs["has_tax_id"].required
            assert specs["text_types"].min_choices == 1
            assert specs["text_types"].max_choices == 5
            assert all(f"sample_{i}_type" in specs for i in range(1, 6))
            assert specs["sample_2"].required and not specs["sample_3"].required
            assert req.address_required and req.documents == ()
            assert specs["privacy_policy_url"].required
            assert "not shared" in specs["privacy_policy_url"].help
            assert "unchecked" in specs["opt_in_description"].help
            assert ("ein" in specs) == (subject == "business")
            if subject == "person":
                assert "not a Hail number" in specs["mobile_phone_number"].help


@pytest.mark.parametrize(
    "field",
    [
        "sender_country",
        "has_tax_id",
        "consent_methods",
        "consent_records",
        "policies_confirmed",
        "has_phone",
        "age_gated",
        "direct_lending",
        "sample_1",
        "terms_url",
    ],
)
def test_new_submissions_require_explicit_facts(field):
    data = {**GOOD}
    del data[field]
    assert field in error_fields(data)


@pytest.mark.parametrize("country", ["GB", "DE", "AU"])
def test_individuals_outside_us_canada_are_ineligible(country):
    assert "sender_country" in error_fields({**GOOD, "sender_country": country})


def test_individual_tax_id_holders_must_use_business():
    assert "has_tax_id" in error_fields({**GOOD, "has_tax_id": "YES"})
    assert not errors({**GOOD, "sender_country": "CA"})


def test_business_requires_a_registration_but_can_operate_abroad():
    assert not errors({**BUSINESS, "sender_country": "GB"}, "business")
    assert not errors(
        {**BUSINESS, "registration_country": "CA", "ein": "123456789"}, "business"
    )
    assert "has_tax_id" in error_fields({**BUSINESS, "has_tax_id": "NO"}, "business")
    assert "ein" in error_fields({**BUSINESS, "ein": "made up"}, "business")


@pytest.mark.parametrize(
    "country,number",
    [
        ("SE", "SE556677889901"),
        ("SE", "556677-8899"),
        ("GB", "SC123456"),
        ("DE", "DE 123 456 789"),
    ],
)
def test_businesses_from_any_country_file_their_own_number(country, number):
    data = {
        **BUSINESS,
        "sender_country": country,
        "registration_country": country,
        "ein": number,
    }
    assert not errors(data, "business")


@pytest.mark.parametrize(
    "country,number",
    [
        ("US", "123456789012"),
        ("CA", "123456789XY"),
        ("SE", "SE1"),
        ("SE", "ABCDEFGH"),
        ("SE", "1" * 21),
        ("se", "556677889901"),
    ],
)
def test_bad_tax_numbers_are_refused(country, number):
    data = {**BUSINESS, "registration_country": country, "ein": number}
    assert error_fields(data, "business") & {"ein", "registration_country"}


@pytest.mark.parametrize(
    "number,country,filed",
    [
        ("12-3456789", "US", "123456789"),
        ("SE 556677-8899 01", "SE", "556677889901"),
        ("se556677889901", "SE", "556677889901"),
        ("SC123456", "GB", "SC123456"),
        ("GB123456789", "GB", "123456789"),
        ("NL 1234.56.789.B01", "NL", "123456789B01"),
        ("US123456789", "US", "US123456789"),
        ("EL 123456789", "GR", "123456789"),
        ("ATU12345678", "AT", "U12345678"),
        ("GR123456789", "GR", "123456789"),
        ("123456789 RT 0001", "CA", "123456789"),
    ],
)
def test_tax_number_files_letters_and_digits(number, country, filed):
    assert tax_number(number, country) == filed


@pytest.mark.parametrize(
    "field,value",
    [
        ("has_phone", "maybe"),
        ("has_links", "yes"),
        ("business_type", "FAKE"),
        ("industry", "FAKE"),
    ],
)
def test_single_choices_are_validated(field, value):
    assert field in error_fields({**BUSINESS, field: value}, "business")


METHODS = {
    "WEBSITE": {
        "website_url": "https://acme.org/book",
        "website_evidence_url": "https://acme.org/evidence",
        "website_action": "Check the optional SMS box.",
    },
    "PRIVATE_FORM": {
        "private_form_url": "https://acme.org/account/book",
        "private_form_evidence_url": "https://acme.org/evidence",
        "private_form_action": "Sign in, select SMS reminders, then save.",
    },
    "VERBAL": {
        "verbal_script": "May Acme send your requested reminder? "
        + GOOD["consent_disclosure"],
        "verbal_evidence_url": "https://acme.org/script",
        "verbal_action": "Staff read the script and record an affirmative yes.",
    },
    "PAPER": {
        "paper_evidence_url": "https://acme.org/blank-form.pdf",
        "paper_action": "Sign the separate SMS consent box on the blank appointment form.",
    },
    "KEYWORD": {
        "keyword_keyword": "REMIND",
        "keyword_number": "+12125550123",
        "keyword_evidence_url": "https://acme.org/poster",
        "keyword_advertisement": "Reception poster: text REMIND for appointment reminders.",
    },
    "QR": {
        "qr_url": "https://acme.org/book",
        "qr_evidence_url": "https://acme.org/qr-poster",
        "qr_advertisement": "Reception poster links to the optional SMS enrollment form.",
    },
    "INBOUND": {
        "inbound_number": "+12125550123",
        "inbound_evidence_url": "https://acme.org/contact",
        "inbound_advertisement": "Text our support team for replies to your appointment question only.",
    },
}


@pytest.mark.parametrize("method", list(METHODS))
def test_every_consent_method_requires_its_own_complete_evidence(method):
    data = {**GOOD, "consent_methods": method, **METHODS[method]}
    assert errors(data) == []
    text = campaign_text(data, "person")
    for name, value in METHODS[method].items():
        assert value in text.message_flow
        missing = {**data}
        del missing[name]
        if name == "verbal_evidence_url":
            continue
        assert name in error_fields(missing)
    assert text.schema_version == 2


def test_verbal_consent_needs_no_screenshot():
    """Carriers ask for the script and the confirmation text, not a picture."""
    data = {**GOOD, "consent_methods": "VERBAL", **METHODS["VERBAL"]}
    del data["verbal_evidence_url"]
    assert errors(data) == []
    assert "evidence url" not in campaign_text(data, "person").message_flow


def test_evidence_help_names_the_upload_route():
    from hailhq.core.providers.verification.sms_form import _method_spec

    assert (
        "POST /verifications/sms/evidence"
        in _method_spec("WEBSITE", "website_evidence_url").help
    )
    assert "Optional" in _method_spec("VERBAL", "verbal_evidence_url").help
    assert "Optional" not in _method_spec("PAPER", "paper_evidence_url").help


def test_every_selected_method_is_validated():
    data = {**GOOD, "consent_methods": "WEBSITE,PAPER"}
    assert {"paper_evidence_url", "paper_action"} <= error_fields(data)
    data.update(METHODS["PAPER"])
    assert not errors(data)
    assert "WEBSITE:" in campaign_text(data, "person").message_flow
    assert "PAPER:" in campaign_text(data, "person").message_flow


@pytest.mark.parametrize(
    "url",
    [
        "acme.org/privacy",
        "http://acme.org/privacy",
        "ftp://acme.org/privacy",
        "https://",
        "https://localhost/x",
        "https://127.0.0.1/x",
        "https://10.1.2.3/x",
        "https://[::1]/x",
        "https://alice:secret@acme.org/x",
        "https://example.com/x",
        "https://assets.example.com/x",
        "https://invalid_host.org/x",
        "https://2130706433/x",
        "https://acme.example/x",
        "https://acme.org:bad/x",
        "https://acme.org:8080/x",
        "https://acme.org/<placeholder>",
    ],
)
def test_public_links_reject_private_credentials_and_placeholders(url):
    assert "website_evidence_url" in error_fields({**GOOD, "website_evidence_url": url})
    assert "privacy_policy_url" in error_fields({**GOOD, "privacy_policy_url": url})


@pytest.mark.parametrize(
    "field", ["consent_optional", "evidence_redacted", "policies_confirmed"]
)
def test_negative_attestations_block_submission(field):
    assert field in error_fields({**GOOD, field: "NO"})


def test_marketing_requires_written_scope_and_rejects_inquiry_or_verbal_enrollment():
    data = {
        **GOOD,
        "text_types": "MARKETING",
        "sample_1_type": "MARKETING",
        "sample_2_type": "MARKETING",
    }
    assert "marketing_consent" in error_fields(data)
    assert not errors({**data, "marketing_consent": "YES"})
    assert "consent_methods" in error_fields(
        {
            **data,
            "marketing_consent": "YES",
            "consent_methods": "INBOUND",
            **METHODS["INBOUND"],
        }
    )


def test_restricted_content_cannot_be_made_eligible_by_age_gate():
    assert "restricted_content" in error_fields(
        {**GOOD, "restricted_content": "RESTRICTED", "age_gated": "YES"}
    )


def test_company_single_purpose_is_not_mixed():
    assert campaign_text(BUSINESS, "business").use_case == "CUSTOMER_CARE"
    assert campaign_text(GOOD, "person").use_case == "SOLE_PROPRIETOR"
    assert (
        campaign_text(
            {**BUSINESS, "text_types": "CUSTOMER_CARE,MARKETING"}, "business"
        ).use_case
        == "MIXED"
    )


@pytest.mark.parametrize("count", range(1, 6))
def test_samples_cover_one_to_five_purposes(count):
    purposes = [
        "CUSTOMER_CARE",
        "ACCOUNT_NOTIFICATION",
        "DELIVERY_NOTIFICATION",
        "SECURITY_ALERT",
        "2FA",
    ][:count]
    data = {**GOOD, "text_types": ",".join(purposes)}
    for i, purpose in enumerate(purposes, 1):
        data[f"sample_{i}"] = (
            f"Acme: {purpose} example for your account. Reply STOP to opt out."
        )
        data[f"sample_{i}_type"] = purpose
    assert not errors(data)
    assert len(campaign_text(data, "person").samples) == max(2, count)
    if count > 1:
        data[f"sample_{count}_type"] = purposes[0]
        assert "text_types" in error_fields(data)


def test_unselected_sample_purposes_are_rejected_and_missing_ones_allowed():
    assert "sample_1_type" in error_fields({**GOOD, "sample_1_type": "MARKETING"})
    assert "sample_3" in error_fields({**GOOD, "sample_3_type": "CUSTOMER_CARE"})
    # A sample without a purpose is fine: purposes are then not checked.
    assert not errors({**GOOD, "sample_3": GOOD["sample_1"]})
    assert not errors({**GOOD, "sample_1_type": "", "sample_2_type": ""})
    # Without purposes: at least one sample for each kind of text.
    three = {
        **GOOD,
        "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION,DELIVERY_NOTIFICATION",
        "sample_1_type": "",
        "sample_2_type": "",
    }
    assert "text_types" in error_fields(three)
    sample = "Acme: Your order 123 shipped today. Reply STOP to opt out."
    assert "text_types" not in error_fields({**three, "sample_3": sample})
    # Mixed: two samples name one kind, one has none, so two kinds lack a sample.
    mixed = {
        **three,
        "sample_1_type": "CUSTOMER_CARE",
        "sample_2_type": "CUSTOMER_CARE",
        "sample_3": sample,
    }
    assert "text_types" in error_fields(mixed)


def test_sample_identity_and_stop_must_be_real():
    assert "sample_1" in error_fields(
        {**GOOD, "sample_1": "Wrong brand: Your appointment is tomorrow. Reply STOP."}
    )
    # The opt-out is needed in one sample, not in each.
    assert not errors(
        {**GOOD, "sample_1": "Acme: Your appointment is tomorrow at 2pm."}
    )
    no_stop = {
        **GOOD,
        "sample_1": "Acme: Your appointment is tomorrow at 2pm.",
        "sample_2": "Acme: Your order 123 shipped today. Thanks!",
    }
    assert "sample_1" in error_fields(no_stop)


def test_the_name_error_quotes_the_program_name():
    data = {
        **GOOD,
        "program_name": "Acme Tech",
        "sample_1": "Hi: your code is 1234. Reply STOP.",
    }
    messages = [message for field, message in errors(data) if field == "sample_1"]
    assert messages == [
        'Add your program name "Acme Tech" to this sample, so people know who is texting.'
    ]


def test_sample_name_check_ignores_spaces_dots_and_case():
    data = {**GOOD, "program_name": "Acme Tech"}
    for name in ("acme.tech", "AcmeTech", "ACME TECH"):
        sample = f"{name}: Your appointment is tomorrow. Reply STOP to opt out."
        assert "sample_1" not in error_fields({**data, "sample_1": sample})
    assert "sample_1" in error_fields(
        {**data, "sample_1": "Acme: Your appointment is tomorrow. Reply STOP."}
    )
    # Both ways: a name without a space matches text with one.
    joined = {**GOOD, "program_name": "AcmeTech"}
    sample = "Acme Tech: your appointment is tomorrow. Reply STOP to opt out."
    assert "sample_1" not in error_fields({**joined, "sample_1": sample})
    # A one-word name keeps the plain match: a handle like @AcmeHQ is fine.
    one = {**GOOD, "sample_1": "@AcmeHQ: your code is 1234. Reply STOP to opt out."}
    assert "sample_1" not in error_fields(one)


def test_sample_link_and_phone_must_match_answers():
    sample = (
        "Acme: Visit https://acme.org or call +1 (212) 555-0123. Reply STOP to opt out."
    )
    data = {**GOOD, "sample_1": sample}
    assert {"has_links", "has_phone"} <= error_fields(data)
    data.update(has_links="YES", has_phone="YES")
    assert not errors(data)
    text = campaign_text(data, "person")
    assert text.has_links and text.has_phone
    assert not text.age_gated and not text.direct_lending


def test_description_samples_and_disclosure_are_not_fabricated():
    data = {
        **GOOD,
        "campaign_description": "Short",
        "sample_1": "Hi",
        "consent_disclosure": "I agree",
        "opt_in_message": "",
    }
    assert {"campaign_description", "sample_1", "consent_disclosure"} <= error_fields(
        data
    )
    text = campaign_text(data, "person")
    assert text.description == "Short" and text.samples[0] == "Hi"
    assert "Consent is not a condition of purchase" not in text.message_flow
    assert text.opt_in_message == ""
    assert (
        campaign_text(
            {
                **GOOD,
                "opt_in_message": "Acme: You have enrolled for appointment reminders.",
            },
            "person",
        ).opt_in_message
        == "Acme: You have enrolled for appointment reminders."
    )


def test_full_flow_is_validated_without_truncating_evidence():
    data = {**GOOD, "opt_in_description": "😀" * 1000}
    assert "opt_in_description" in error_fields(data)
    assert text_length(campaign_text(data, "person").message_flow) > 2048
    assert GOOD["website_evidence_url"] in campaign_text(data, "person").message_flow


def test_legacy_saved_application_compiles_without_invented_claims():
    data = {
        "campaign_description": "Order updates",
        "sample_1": "Hi",
        "sample_2": "Bye",
        "opt_in_description": "Web form",
        "has_links": "NO",
        "privacy_policy_url": "https://acme.example/privacy",
        "terms_url": "https://acme.example/terms",
        # A vetted filing: an amend keeps the filed answer (blank is low volume).
        "daily_texts": "OVER_2000",
    }
    text = campaign_text(data, "business")
    assert text.schema_version == 1
    assert text.description == "Order updates" and text.message_flow == "Web form"
    assert text.samples == ["Hi", "Bye"] and text.use_case == "MIXED"
    assert "sender_country" in error_fields(data, "business")


def test_limits_count_utf16_and_preserve_provider_limits():
    fields = {f.name: f for f in sms_requirements(TELNYX, "person").fields}
    assert fields["campaign_description"].max_length == 4096
    assert fields["sample_5"].max_length == 1024
    assert fields["email"].max_length == 100
    for suffix, expected in [("😀", []), ("a😀", ["sample_1"])]:
        result = length_problems(
            sms_requirements(TELNYX, "person"),
            {**GOOD, "sample_1": "s" * 1022 + suffix},
            {},
        )
        assert [p.field for p in result] == expected


def test_purpose_choices_validate_allowlist_and_maximum():
    assert "text_types" in error_fields({**GOOD, "text_types": "MADE_UP"})
    assert "text_types" in error_fields(
        {
            **GOOD,
            "text_types": "2FA,MARKETING,FRAUD_ALERT,SECURITY_ALERT,POLLING_VOTING,CUSTOMER_CARE",
        }
    )
    assert campaign_text(
        {**GOOD, "text_types": " 2FA, MARKETING,2FA "}, "person"
    ).text_types == ["2FA", "MARKETING"]


def test_every_sms_carrier_names_its_campaign_key():
    from hailhq.core.providers.verification import _FACTORIES
    from hailhq.core.providers.verification.base import SmsRegistrationProvider

    sms = {name: f for (name, purpose), f in _FACTORIES.items() if purpose == "sms"}
    assert sms
    for name, factory in sms.items():
        assert isinstance(factory, type), f"{name}: register the plug-in class"
        assert issubclass(factory, SmsRegistrationProvider), name
        ref = getattr(factory, "campaign_ref", None)
        assert isinstance(ref, str) and ref, f"{name}: set campaign_ref as a string"


def test_age_gating_needs_a_real_process_and_lending_needs_review():
    data = {**GOOD, "age_gated": "YES"}
    assert {
        "age_verification_description",
        "age_verification_evidence_url",
    } <= error_fields(data)
    data.update(
        age_verification_description="Before enrollment staff verify the minimum age with an identity check; no identity documents are published.",
        age_verification_evidence_url="https://acme.org/age-flow",
    )
    assert not errors(data)
    assert campaign_text(data, "person").age_gated
    assert "https://acme.org/age-flow" in campaign_text(data, "person").message_flow
    assert "direct_lending" in error_fields({**GOOD, "direct_lending": "YES"})
    assert campaign_text({**GOOD, "direct_lending": "YES"}, "person").direct_lending


def test_dates_and_short_login_codes_are_not_treated_as_phone_numbers():
    data = {
        **GOOD,
        "sample_1": "Acme: Your code is 12345678, valid on 2026-11-05. Reply STOP to opt out.",
    }
    assert "has_phone" not in error_fields(data)


@pytest.mark.parametrize(
    "change",
    [
        {"message_frequency": "20 promotional texts daily"},
        {
            "consent_disclosure": "Acme reminders. Message and data rates may apply. STOP HELP https://acme.org/privacy https://acme.org/terms"
        },
        {
            "consent_disclosure": "Acme once per appointment. Our service rates are low. STOP HELP https://acme.org/privacy https://acme.org/terms"
        },
    ],
)
def test_disclosure_requires_actual_cadence_and_message_data_rates(change):
    assert "consent_disclosure" in error_fields({**GOOD, **change})


@pytest.mark.parametrize(
    "rates",
    [
        "Message and data rates",
        "Msg & data rates",
        "Msg. & data charges",
        "Message/data rates",
    ],
)
def test_disclosure_accepts_equivalent_rate_wording(rates):
    data = {
        **GOOD,
        "consent_disclosure": GOOD["consent_disclosure"].replace(
            "Message and data rates", rates
        ),
    }
    assert not errors(data)


def test_disclosure_purpose_coverage_requires_affirmative_fact():
    assert "disclosure_purposes_confirmed" in error_fields(
        {**GOOD, "disclosure_purposes_confirmed": "NO"}
    )
    missing = {**GOOD}
    del missing["disclosure_purposes_confirmed"]
    assert "disclosure_purposes_confirmed" in error_fields(missing)


@pytest.mark.parametrize(
    "keyword",
    ["STOP", "stopall", "UNSUBSCRIBE", "Cancel", "END", "QUIT", "HELP", "INFO"],
)
def test_reserved_help_and_opt_out_keywords_cannot_enroll_recipients(keyword):
    data = {
        **GOOD,
        "consent_methods": "KEYWORD",
        **METHODS["KEYWORD"],
        "keyword_keyword": keyword,
    }
    assert "keyword_keyword" in error_fields(data)


def test_keyword_enrollment_requires_actual_confirmation_before_submission():
    fields = {
        **GOOD,
        "consent_methods": "KEYWORD",
        **METHODS["KEYWORD"],
        "opt_in_message": "",
    }
    assert "opt_in_message" in error_fields(fields, provider=TWILIO)
    fields["opt_in_message"] = GOOD["opt_in_message"]
    assert not errors(fields, provider=TWILIO)
    assert campaign_text(fields, "person").opt_in_keywords == [
        fields["keyword_keyword"]
    ]


@pytest.mark.parametrize(
    "missing,replace",
    [
        ('program name "Acme"', ("Acme", "Other sender")),
        ("frequency", ("once per appointment", "regularly")),
        ("rates", ("Msg and data rates may apply.", "Our service rates are low.")),
        ("HELP", ("HELP for help", "helpful assistance")),
        ("STOP", ("STOP to stop", "nonstop updates")),
    ],
)
def test_confirmation_requires_real_sender_cadence_rates_and_help_stop(
    missing, replace
):
    confirmation = GOOD["opt_in_message"].replace(*replace)
    problems = errors({**GOOD, "opt_in_message": confirmation})
    assert any(
        field == "opt_in_message" and missing.casefold() in message.casefold()
        for field, message in problems
    )
    # Validation must never repair the filed response by adding unsupported copy.
    assert (
        campaign_text({**GOOD, "opt_in_message": confirmation}, "person").opt_in_message
        == confirmation
    )


@pytest.mark.parametrize(
    "rates",
    [
        "Msg & data rates",
        "Msg. & data charges",
        "Message and data rates",
        "Message/data rates",
    ],
)
def test_confirmation_accepts_real_rate_abbreviations(rates):
    confirmation = GOOD["opt_in_message"].replace("Msg and data rates", rates)
    assert not errors({**GOOD, "opt_in_message": confirmation})


@pytest.mark.parametrize(
    "flag", ["has_links", "has_phone", "age_gated", "direct_lending"]
)
@pytest.mark.parametrize("answer", ["YES", "NO"])
def test_content_flag_whitespace_is_identical_in_validation_and_filing(flag, answer):
    plain = {**GOOD, flag: answer}
    padded = {**GOOD, flag: f"  {answer}\n"}
    assert errors(padded) == errors(plain)
    assert getattr(campaign_text(padded, "person"), flag) is (answer == "YES")


def test_attestation_whitespace_does_not_change_compiled_flow():
    data = {**GOOD, "consent_optional": " YES ", "marketing_consent": " YES "}
    assert errors(data) == errors(
        {**data, "consent_optional": "YES", "marketing_consent": "YES"}
    )
    assert (
        campaign_text(data, "person").message_flow
        == campaign_text(
            {**data, "consent_optional": "YES", "marketing_consent": "YES"}, "person"
        ).message_flow
    )


def test_tax_number_help_names_the_vat_countries():
    from hailhq.core.providers.verification.sms_form import VAT_COUNTRIES

    help_text = next(
        f.help for f in sms_requirements(TELNYX, "business").fields if f.name == "ein"
    )
    assert all(code in help_text for code in VAT_COUNTRIES)
    assert "GB, DE and FR, use their company registration number" in help_text
    assert "GB" not in VAT_COUNTRIES and "DE" not in VAT_COUNTRIES


@pytest.mark.parametrize("provider", [TELNYX, TWILIO])
def test_companies_say_their_daily_texts_and_persons_never_see_it(provider):
    specs = {f.name: f for f in sms_requirements(provider, "business").fields}
    daily = specs["daily_texts"]
    assert not daily.required
    assert daily.label == "How many texts do you expect to send each day?"
    assert [(o.key, o.label) for o in daily.options] == [
        ("NOT_SURE", "Not sure yet"),
        ("UP_TO_2000", "Up to 2,000"),
        ("OVER_2000", "More than 2,000"),
    ]
    assert "$" not in daily.help
    for carrier in ("Twilio", "Telnyx", "TCR", "T-Mobile"):
        assert carrier not in daily.help
    assert "Sending more later needs a carrier check." in daily.help
    person = {f.name for f in sms_requirements(provider, "person").fields}
    assert not {"daily_texts", "faster_lane_ok"} & person


def test_only_a_carrier_with_a_faster_lane_asks_for_it():
    lane = {f.name: f for f in sms_requirements(TELNYX, "business").fields}[
        "faster_lane_ok"
    ]
    assert not lane.required and lane.kind == "choices"
    assert lane.label == (
        "Move me to the faster lane automatically if the networks need more "
        "details about my company."
    )
    assert [o.key for o in lane.options] == ["YES"] and lane.max_choices == 1
    assert "$" not in lane.help and "Telnyx" not in lane.help
    assert TELNYX.faster_lane and not TWILIO.faster_lane
    twilio = {f.name for f in sms_requirements(TWILIO, "business").fields}
    assert "faster_lane_ok" not in twilio


def test_daily_texts_and_faster_lane_are_optional_but_checked():
    blank = {
        k: v for k, v in BUSINESS.items() if k not in ("daily_texts", "faster_lane_ok")
    }
    assert not errors(blank, "business")
    assert "daily_texts" in error_fields(
        {**BUSINESS, "daily_texts": "MAYBE"}, "business"
    )
    assert "faster_lane_ok" in error_fields(
        {**BUSINESS, "faster_lane_ok": "NO"}, "business"
    )
    assert not errors({**BUSINESS, "faster_lane_ok": "YES"}, "business")
    # A person's form has neither field: an answer there is ignored.
    assert not errors({**GOOD, "daily_texts": "MAYBE", "faster_lane_ok": "YES"})


@pytest.mark.parametrize("answer", [None, "", "NOT_SURE", "UP_TO_2000"])
def test_blank_not_sure_and_up_to_2000_are_low_volume(answer):
    from hailhq.core.providers.verification.sms_form import faster_lane_ok, low_volume

    fields = {k: v for k, v in BUSINESS.items() if k != "daily_texts"}
    if answer is not None:
        fields["daily_texts"] = answer
    assert low_volume(fields, "business") is True
    one = campaign_text(fields, "business")
    assert one.use_case == "LOW_VOLUME" and one.text_types == ["CUSTOMER_CARE"]
    two = campaign_text({**fields, "text_types": "CUSTOMER_CARE,MARKETING"}, "business")
    assert two.use_case == "LOW_VOLUME"
    assert two.text_types == ["CUSTOMER_CARE", "MARKETING"]
    assert faster_lane_ok(fields, "business") is False
    # Read as a choices value, like its check: repeats and commas are fine.
    for ticked in ("YES", "YES,", "YES, YES", " YES "):
        assert faster_lane_ok({**fields, "faster_lane_ok": ticked}, "business")
    assert not errors({**BUSINESS, "faster_lane_ok": "YES, YES"}, "business")


def test_over_2000_and_persons_are_never_low_volume():
    from hailhq.core.providers.verification.sms_form import faster_lane_ok, low_volume

    assert low_volume(BUSINESS, "business") is False
    assert campaign_text(BUSINESS, "business").use_case == "CUSTOMER_CARE"
    # The box means nothing on the vetted path.
    assert faster_lane_ok({**BUSINESS, "faster_lane_ok": "YES"}, "business") is False
    person = {**GOOD, "daily_texts": "UP_TO_2000", "faster_lane_ok": "YES"}
    assert low_volume(person, "person") is False
    assert faster_lane_ok(person, "person") is False
    assert campaign_text(person, "person").use_case == "SOLE_PROPRIETOR"


# The consent disclosure names the two policies


LINKED = (
    "Get Acme appointment reminders, once per appointment. Message and data "
    "rates may apply. Reply STOP to opt out or HELP for help. See our {privacy} "
    "and {terms}."
)


def test_disclosure_accepts_the_policy_web_addresses():
    assert "consent_disclosure" not in error_fields(GOOD)


@pytest.mark.parametrize(
    "terms", ["SMS Terms", "Terms", "Terms of Service", "terms and conditions"]
)
def test_disclosure_accepts_policies_named_by_their_link_words(terms):
    # A real checkbox links the words, so a true copy has no web address.
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(privacy="Privacy Policy", terms=terms),
    }
    assert "consent_disclosure" not in error_fields(data)


@pytest.mark.parametrize(
    "privacy,terms,missing,present",
    [
        ("Privacy Policy", "rules", "SMS terms", "privacy policy"),
        ("notice", "SMS Terms", "privacy policy", "SMS terms"),
    ],
)
def test_disclosure_names_the_one_missing_policy(privacy, terms, missing, present):
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(privacy=privacy, terms=terms),
    }
    (message,) = [m for f, m in errors(data) if f == "consent_disclosure"]
    assert missing in message
    assert present not in message
    # Only what is missing, not every part.
    for part in ("program name", "frequency", "message and data rates", "STOP"):
        assert part not in message


def test_disclosure_word_match_needs_a_whole_word():
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(
            privacy="Privacy Policy", terms="what determines your plan"
        ),
    }
    (message,) = [m for f, m in errors(data) if f == "consent_disclosure"]
    assert "SMS terms" in message


def test_disclosure_error_lists_every_missing_part():
    data = {**GOOD, "consent_disclosure": "Sign up for texts."}
    (message,) = [m for f, m in errors(data) if f == "consent_disclosure"]
    assert message.startswith('The disclosure is missing your program name "Acme", ')
    for part in (
        '"once per appointment"',
        "message and data rates",
        "STOP",
        "HELP",
        "privacy policy",
        "SMS terms",
    ):
        assert part in message


def test_compiled_text_carries_both_policy_urls_to_each_carrier():
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(
            privacy="Privacy Policy", terms="SMS Terms"
        ),
    }
    assert not errors(data)
    text = asdict(campaign_text(data, "person"))
    assert text["privacy_policy_url"] == "https://acme.org/privacy"
    assert text["terms_url"] == "https://acme.org/terms"
    # Reviewers read these carrier fields, so the disclosure need not repeat them.
    assert twilio_sms._policy_links(text) == {
        "privacy_policy_url": "https://acme.org/privacy",
        "terms_and_conditions_url": "https://acme.org/terms",
    }
    assert telnyx_sms._policy_links(text) == {
        "privacyPolicyLink": "https://acme.org/privacy",
        "termsAndConditionsLink": "https://acme.org/terms",
    }


@pytest.mark.parametrize(
    "privacy,terms,missing",
    [
        ("how we respect your privacy", "SMS Terms", "privacy policy"),
        ("Privacy Policy", "rules, in terms of frequency,", "SMS terms"),
        ("Privacy Policy", "rules. Terms apply", "SMS terms"),
        ("Privacy Policy", "rules on our terms", "SMS terms"),
        ("Privacy Policy", "rules. By texting terms like HELP", "SMS terms"),
        ("Privacy Policy", "rules. Our refund policy and terms apply", "SMS terms"),
    ],
)
def test_disclosure_words_must_name_a_policy(privacy, terms, missing):
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(privacy=privacy, terms=terms),
    }
    (message,) = [m for f, m in errors(data) if f == "consent_disclosure"]
    assert missing in message


def test_policy_words_still_need_the_policy_web_addresses():
    # The words pass the disclosure, and the addresses go to the carrier.
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(
            privacy="Privacy Policy", terms="SMS Terms"
        ),
        "terms_url": "",
    }
    fields = error_fields(data)
    assert "terms_url" in fields
    assert "consent_disclosure" not in fields


@pytest.mark.parametrize(
    "privacy,terms",
    [
        ("Privacy & Cookie Policy", "SMS Terms"),
        ("Privacy Policy", "SMS T&Cs"),
        ("Privacy Policy", "Terms&Conditions"),
        ("Privacy Policy &", "Terms"),
    ],
)
def test_disclosure_accepts_common_policy_spellings(privacy, terms):
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(privacy=privacy, terms=terms).replace(
            "& and", "&"
        ),
    }
    assert "consent_disclosure" not in error_fields(data)


def test_disclosure_accepts_terms_named_before_the_privacy_policy():
    data = {
        **GOOD,
        "consent_disclosure": LINKED.format(
            privacy="Terms and Privacy Policy", terms="rules"
        ),
    }
    assert "consent_disclosure" not in error_fields(data)
