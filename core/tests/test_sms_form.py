import pytest

from hailhq.core.providers.verification.forms import length_problems, text_length
from hailhq.core.providers.verification.sms_form import (
    campaign_text,
    field_problems,
    sms_requirements,
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
    "campaign_description": "Acme sends appointment reminders and replies to customer support questions.",
    "message_frequency": "One reminder per appointment; replies when requested.",
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


def test_business_requires_supported_registration_but_can_operate_abroad():
    assert not errors({**BUSINESS, "sender_country": "GB"}, "business")
    assert not errors(
        {**BUSINESS, "registration_country": "CA", "ein": "123456789"}, "business"
    )
    assert "registration_country" in error_fields(
        {**BUSINESS, "registration_country": "GB"}, "business"
    )
    assert "has_tax_id" in error_fields({**BUSINESS, "has_tax_id": "NO"}, "business")
    assert "ein" in error_fields({**BUSINESS, "ein": "made up"}, "business")


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
        assert name in error_fields(missing)
    assert text.schema_version == 2


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


def test_unselected_and_missing_sample_mapping_are_rejected():
    assert "sample_1_type" in error_fields({**GOOD, "sample_1_type": "MARKETING"})
    assert "sample_3" in error_fields({**GOOD, "sample_3_type": "CUSTOMER_CARE"})
    assert "sample_3_type" in error_fields({**GOOD, "sample_3": GOOD["sample_1"]})


def test_sample_identity_and_stop_must_be_real():
    assert "sample_1" in error_fields(
        {**GOOD, "sample_1": "Wrong brand: Your appointment is tomorrow. Reply STOP."}
    )
    assert "sample_1" in error_fields(
        {**GOOD, "sample_1": "Acme: Your appointment is tomorrow at 2pm."}
    )


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
