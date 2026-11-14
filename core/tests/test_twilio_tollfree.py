import json

import pytest
from hailhq.core.providers.verification.base import VerificationProviderError
from twilio.http.response import Response

from .test_twilio_sms_registration import ADDRESS, FIELDS
from .test_twilio_sms_sdk import sdk_provider

TF_FIELDS = {
    **FIELDS,
    "registration_kind": "toll_free",
    "message_volume": "1000",
    "business_registration_authority": "EIN",
}


async def test_tollfree_requirements_are_distinct_and_have_no_otp():
    p, _ = sdk_provider()
    requirements = await p.requirements("CA", "toll_free", "business")
    assert requirements.number_type == "toll_free"
    assert requirements.country_code == "CA"
    assert {
        "registration_kind",
        "message_volume",
        "business_registration_authority",
    } <= {f.name for f in requirements.fields}
    assert (
        p.confirmation({"registration_kind": "toll_free", "subject_type": "person"})
        is None
    )


async def test_tollfree_sdk_files_end_customer_evidence_and_polls_one_number():
    p, http = sdk_provider()
    original = http.request

    def request(method, uri, **kwargs):
        if "Tollfree/Verifications" in uri:
            http.calls.append((method, uri, kwargs))
            if method == "GET" and uri.endswith("/Verifications"):
                return Response(
                    200,
                    json.dumps({"verifications": [], "meta": {"key": "verifications"}}),
                )
            return Response(
                200,
                json.dumps(
                    {
                        "sid": "HH1",
                        "status": "TWILIO_APPROVED",
                        "tollfree_phone_number_sid": "PN1",
                    }
                ),
            )
        return original(method, uri, **kwargs)

    http.request = request
    req = await p.requirements("US", "toll_free", "business")
    result = await p.create_draft(
        organization_id="org",
        contact_email="ops@hail.so",
        requirements=req,
        fields=TF_FIELDS,
        address=ADDRESS,
        documents={},
        provider_resource_id="PN1",
        e164="+18005550100",
    )
    assert not result.problems
    assert result.refs["tollfree_verification_sid"] == "HH1"
    assert not any("BrandRegistrations" in uri for _, uri, _ in http.calls)
    data = next(
        kwargs["data"]
        for method, uri, kwargs in http.calls
        if method == "POST" and "Tollfree/Verifications" in uri
    )
    assert data["BusinessName"] == "Acme Inc"
    assert data["BusinessContactEmail"] == FIELDS["rep_email"]
    assert data["TollfreePhoneNumberSid"] == "PN1"
    assert data["OptInImageUrls"] == [FIELDS["website_evidence_url"]]
    assert data["PrivacyPolicyUrl"] == FIELDS["privacy_policy_url"]
    assert data["TermsAndConditionsUrl"] == FIELDS["terms_url"]
    assert (await p.status(result.refs)).state == "approved"
    await p.attach_number(result.refs, provider_resource_id="PN1", e164="+18005550100")
    with pytest.raises(VerificationProviderError):
        await p.attach_number(
            result.refs, provider_resource_id="PN2", e164="+18005550101"
        )


@pytest.mark.parametrize(
    "changes,field",
    [
        ({"message_volume": "-1"}, "message_volume"),
        (
            {"consent_methods": "VERBAL", "verbal_evidence_url": ""},
            "verbal_evidence_url",
        ),
        ({"consent_methods": "INBOUND"}, "consent_methods"),
        (
            {"business_registration_authority": "WRONG"},
            "business_registration_authority",
        ),
    ],
)
async def test_tollfree_preflight_stops_unsupported_or_incomplete_answers(
    changes, field
):
    p, http = sdk_provider()
    assert field in {
        problem.field
        for problem in p.validate_application(
            {**TF_FIELDS, **changes}, ADDRESS, "business"
        )
    }
    assert http.calls == []


async def test_editable_tollfree_rejection_is_repaired_on_same_request():
    p, http = sdk_provider()
    status = "TWILIO_REJECTED"

    def request(method, uri, **kwargs):
        http.calls.append((method, uri, kwargs))
        return Response(
            200,
            json.dumps(
                {
                    "sid": "HH1",
                    "status": status,
                    "tollfree_phone_number_sid": "PN1",
                    "edit_allowed": True,
                    "rejection_reason": "opt-in evidence",
                    "error_code": 30513,
                }
            ),
        )

    http.request = request
    refs = {
        "registration_kind": "toll_free",
        "tollfree_verification_sid": "HH1",
        "provider_resource_id": "PN1",
        "e164": "+18005550100",
    }
    assert (await p.status(refs)).state == "pending"
    assert refs["needs_information"]
    assert p.step(refs) == "needs_information"
    await p.amend_filed_application(refs, TF_FIELDS, ADDRESS, "business")
    updates = [
        (uri, kwargs["data"]) for method, uri, kwargs in http.calls if method == "POST"
    ]
    assert len(updates) == 1
    assert updates[0][0].endswith("/Tollfree/Verifications/HH1")
    assert "TollfreePhoneNumberSid" not in updates[0][1]


async def test_lost_tollfree_create_response_is_recovered_without_second_submission():
    import requests

    p, http = sdk_provider()
    saved = []

    def request(method, uri, **kwargs):
        http.calls.append((method, uri, kwargs))
        if method == "GET":
            return Response(
                200,
                json.dumps({"verifications": saved, "meta": {"key": "verifications"}}),
            )
        saved.append(
            {
                "sid": "HH1",
                "tollfree_phone_number_sid": "PN1",
                "external_reference_id": kwargs["data"]["ExternalReferenceId"],
            }
        )
        raise requests.Timeout("response lost")

    http.request = request
    req = await p.requirements("US", "toll_free", "business")
    kwargs = {
        "organization_id": "org",
        "contact_email": "ops@hail.so",
        "requirements": req,
        "fields": TF_FIELDS,
        "address": ADDRESS,
        "documents": {},
        "provider_resource_id": "PN1",
        "e164": "+18005550100",
    }
    with pytest.raises(VerificationProviderError):
        await p.create_draft(**kwargs)
    recovered = await p.create_draft(**kwargs)
    assert recovered.refs["tollfree_verification_sid"] == "HH1"
    assert sum(method == "POST" for method, _, _ in http.calls) == 1


async def test_lost_amendment_response_recovers_matching_saved_carrier_answers():
    import requests
    from hailhq.core.providers.verification import twilio_tollfree

    p, http = sdk_provider()
    fields = {
        **TF_FIELDS,
        "consent_records": "Consent is recorded with the disclosure version and timestamp.",
    }
    saved = {
        "sid": "HH1",
        "status": "TWILIO_REJECTED",
        "tollfree_phone_number_sid": "PN1",
        "edit_allowed": True,
        "notification_email": "ops@hail.so",
    }

    def request(method, uri, **kwargs):
        http.calls.append((method, uri, kwargs))
        if method == "POST":
            saved.update(
                twilio_tollfree.payload(
                    fields, ADDRESS, "business", "ops@hail.so", "PN1"
                )
            )
            saved["status"] = "PENDING_REVIEW"
            raise requests.Timeout("response lost")
        return Response(200, json.dumps(saved))

    http.request = request
    refs = {
        "registration_kind": "toll_free",
        "tollfree_verification_sid": "HH1",
        "provider_resource_id": "PN1",
        "needs_information": True,
    }
    with pytest.raises(VerificationProviderError):
        await p.amend_filed_application(refs, fields, ADDRESS, "business")
    await p.amend_filed_application(refs, fields, ADDRESS, "business")
    assert sum(method == "POST" for method, _, _ in http.calls) == 1


def test_verification_codes_use_the_current_official_tollfree_category():
    # https://www.twilio.com/docs/messaging/compliance/toll-free/api-onboarding
    # Current TFV documentation names TWO_FACTOR_AUTHENTICATION, not 2FA.
    from hailhq.core.providers.verification import twilio_tollfree

    data = twilio_tollfree.payload(
        {**TF_FIELDS, "text_types": "2FA"}, ADDRESS, "business", "ops@hail.so", "PN1"
    )
    assert data["use_case_categories"] == ["TWO_FACTOR_AUTHENTICATION"]
