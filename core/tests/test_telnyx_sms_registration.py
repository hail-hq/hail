import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.core.providers.verification.base import (
    Address,
    Problem,
    VerificationProviderError,
)
from hailhq.core.providers.verification.telnyx_sms import (
    TelnyxSmsRegistrationProvider,
)

FIELDS = {
    "legal_name": "Acme Inc",
    "business_type": "PRIVATE_PROFIT",
    "ein": "12-3456789",
    "industry": "RETAIL",
    "website": "https://acme.example",
    "rep_name": "Ada Lovelace",
    "rep_email": "ada@acme.example",
    "rep_phone": "+15125550100",
    "rep_title": "CEO",
    "campaign_description": "Order updates and delivery notices for customers who ordered on our site.",
    "sample_1": "Acme: your order 123 shipped. Reply STOP to opt out.",
    "sample_2": "Acme: your order 123 was delivered. Reply STOP to opt out.",
    "opt_in_description": "Customers tick a box at checkout on acme.example.",
    "has_links": "NO",
    "privacy_policy_url": "https://acme.example/privacy",
    "terms_url": "https://acme.example/terms",
}
ADDRESS = Address(
    customer_name="Acme Inc",
    street="1 Main St",
    city="Austin",
    region="TX",
    postal_code="78701",
    country_code="US",
)


def _provider(handler):
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://api.telnyx.com/v2"
    )
    return TelnyxSmsRegistrationProvider(api_key="k", client=client)


async def _draft(p, subject="business", fields=FIELDS):
    req = await p.requirements("US", "local", subject)
    return await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=fields,
        address=ADDRESS,
        documents={},
        messaging_service_sid="MP1",
    )


async def test_business_draft_creates_brand_and_requests_vetting() -> None:
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(
                200, json={"brandId": "B1", "identityStatus": "SELF_DECLARED"}
            )
        if req.url.path.endswith("/vetting"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    draft = await _draft(_provider(handler))
    assert draft.problems == []
    assert (
        draft.refs["brand_id"] == "B1" and draft.refs["messaging_profile_id"] == "MP1"
    )
    assert draft.refs["subject_type"] == "business"
    assert "Acme" not in repr({k: v for k, v in draft.refs.items() if k != "campaign"})
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/v2/10dlc/brand")
    assert body["entityType"] == "PRIVATE_PROFIT" and body["ein"] == "123456789"
    assert body["country"] == "US" and body["state"] == "TX"
    assert body["website"] == "https://acme.example"
    assert calls[1][:2] == ("POST", "/v2/10dlc/brand/B1/vetting")
    assert calls[1][2] == {"vettingProvider": "AEGIS", "vettingClass": "STANDARD"}


async def test_bad_link_is_a_problem_and_sends_nothing() -> None:
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    draft = await _draft(
        _provider(handler), fields={**FIELDS, "terms_url": "acme.example/terms"}
    )
    assert [p.field for p in draft.problems] == ["terms_url"]
    assert draft.refs == {} and calls == []


async def test_missing_field_is_a_problem_and_sends_nothing() -> None:
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    draft = await _draft(_provider(handler), fields={**FIELDS, "ein": ""})
    assert [p.field for p in draft.problems] == ["ein"]
    assert calls == []


async def test_status_creates_campaign_once_identity_verified() -> None:
    posted = []

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"identityStatus": "VETTED_VERIFIED", "failureReasons": None}
            )
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    p = _provider(handler)
    refs = {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "campaign": {
            "use_case": "MIXED",
            "description": "d" * 40,
            "message_flow": "f" * 40,
            "samples": ["s" * 20, "t" * 20],
            "has_links": False,
            "privacy_policy_url": "https://acme.example/privacy",
            "terms_url": "https://acme.example/terms",
        },
    }
    st = await p.status(refs)
    assert st.state == "pending" and refs["campaign_id"] == "C1"
    assert posted[0]["usecase"] == "MIXED" and posted[0]["brandId"] == "B1"
    assert posted[0]["sample1"] == "s" * 20 and posted[0]["embeddedLink"] is False
    assert posted[0]["optoutKeywords"] == "STOP,STOPALL,UNSUBSCRIBE,CANCEL,END,QUIT"
    assert posted[0]["privacyPolicyLink"] == "https://acme.example/privacy"
    assert posted[0]["termsAndConditionsLink"] == "https://acme.example/terms"


async def test_status_files_an_old_snapshot_without_links() -> None:
    """A draft filed before the links existed still reaches the carrier."""
    posted = []

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"identityStatus": "VETTED_VERIFIED", "failureReasons": None}
            )
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    refs = {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "campaign": {
            "use_case": "MIXED",
            "description": "d" * 40,
            "message_flow": "f" * 40,
            "samples": ["s" * 20, "t" * 20],
            "has_links": False,
        },
    }
    assert (await _provider(handler).status(refs)).state == "pending"
    assert "privacyPolicyLink" not in posted[0]


async def test_status_waits_while_identity_not_verified() -> None:
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(req)
        return httpx.Response(200, json={"identityStatus": "UNVERIFIED"})

    p = _provider(handler)
    refs = {"brand_id": "B1", "subject_type": "business", "campaign": {}}
    assert (await p.status(refs)).state == "pending"
    assert posted == [] and "campaign_id" not in refs


async def test_status_rejected_brand() -> None:
    p = _provider(
        lambda req: httpx.Response(
            200, json={"identityStatus": "UNVERIFIED", "failureReasons": "EIN mismatch"}
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "business", "campaign": {}})
    assert st.state == "rejected" and "EIN" in st.reason


async def test_status_approved_and_rejected_from_campaign() -> None:
    state = {"campaignStatus": "MNO_ACCEPTED", "failureReasons": None}

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "VERIFIED"})
        if req.url.path.endswith("/10dlc/campaign/C1"):
            return httpx.Response(200, json=state)
        return httpx.Response(404)

    p = _provider(handler)
    refs = {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "campaign_id": "C1",
        "subject_type": "business",
        "campaign": {},
    }
    assert (await p.status(refs)).state == "approved"
    state.update({"campaignStatus": "TCR_PENDING"})
    assert (await p.status(refs)).state == "pending"
    state.update(
        {
            "campaignStatus": "TCR_FAILED",
            "failureReasons": "Sample messages missing opt-out language",
        }
    )
    st = await p.status(refs)
    assert st.state == "rejected" and "opt-out" in st.reason


async def test_status_server_error_raises() -> None:
    p = _provider(lambda req: httpx.Response(503))
    with pytest.raises(VerificationProviderError):
        await p.status({"brand_id": "B1", "subject_type": "business", "campaign": {}})


async def test_4xx_gives_neutral_problem_and_cleans_up() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand") and req.method == "POST":
            return httpx.Response(200, json={"brandId": "B1"})
        if req.url.path.endswith("/vetting"):
            return httpx.Response(
                422,
                json={"errors": [{"detail": "Telnyx says the EIN 123456789 is bad"}]},
            )
        return httpx.Response(200, json={})

    draft = await _draft(_provider(handler))
    assert draft.refs == {}
    assert len(draft.problems) == 1
    msg = draft.problems[0].message
    assert "123456789" not in msg and "Telnyx" not in msg
    assert ("DELETE", "/v2/10dlc/brand/B1") in calls


async def test_5xx_raises_and_cleans_up() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand") and req.method == "POST":
            return httpx.Response(200, json={"brandId": "B1"})
        if req.url.path.endswith("/vetting"):
            return httpx.Response(500)
        return httpx.Response(200, json={})

    with pytest.raises(VerificationProviderError):
        await _draft(_provider(handler))
    assert ("DELETE", "/v2/10dlc/brand/B1") in calls


async def test_unexpected_error_cleans_up() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={})  # no brandId
        return httpx.Response(200, json={})

    with pytest.raises(VerificationProviderError):
        await _draft(_provider(handler))


async def test_attach_posts_and_conflict_is_success() -> None:
    calls = []
    status = {"code": 200}

    def handler(req):
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(status["code"], json={})

    p = _provider(handler)
    await p.attach_number(
        {"campaign_id": "C1"}, provider_resource_id="PN1", e164="+15125550100"
    )
    assert calls[-1] == (
        "POST",
        "/v2/10dlc/phone_number_campaigns",
        {"phoneNumber": "+15125550100", "campaignId": "C1"},
    )
    status["code"] = 409
    await p.attach_number(
        {"campaign_id": "C1"}, provider_resource_id="PN1", e164="+15125550100"
    )
    status["code"] = 500
    with pytest.raises(VerificationProviderError):
        await p.attach_number(
            {"campaign_id": "C1"}, provider_resource_id="PN1", e164="+15125550100"
        )


async def test_detach_deletes_the_number_campaign_link() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.raw_path))
        return httpx.Response(200, json={})

    await _provider(handler).detach_number(
        {}, provider_resource_id="PN1", e164="+15125550100"
    )
    assert calls == [("DELETE", b"/v2/10dlc/phone_number_campaigns/%2B15125550100")]


async def test_detach_not_linked_does_not_raise() -> None:
    def handler(req):
        return httpx.Response(404, json={})

    await _provider(handler).detach_number(
        {}, provider_resource_id="PN1", e164="+15125550100"
    )


async def test_discard_order_and_never_raises() -> None:
    calls = []
    codes = {"campaign": 500}

    def handler(req):
        calls.append((req.method, req.url.path))
        if "/campaign/" in req.url.path:
            return httpx.Response(codes["campaign"], json={})
        return httpx.Response(200, json={})

    p = _provider(handler)
    await p.discard({"brand_id": "B1", "campaign_id": "C1"})
    assert calls == [
        ("DELETE", "/v2/10dlc/campaign/C1"),
        ("DELETE", "/v2/10dlc/brand/B1"),
    ]
    codes["campaign"] = 404
    await p.discard({"brand_id": "B1", "campaign_id": "C1"})
    await p.discard({})


@pytest.mark.parametrize(
    ("campaign_status", "state"),
    [
        ("TCR_ACCEPTED", "pending"),
        ("TCR_PENDING", "pending"),
        ("MNO_PENDING", "pending"),
        ("MNO_ACCEPTED", "approved"),
        ("MNO_PROVISIONED", "approved"),
        ("ACTIVE", "approved"),
        ("TCR_FAILED", "rejected"),
        ("TELNYX_FAILED", "rejected"),
        ("MNO_REJECTED", "rejected"),
        ("MNO_PROVISIONING_FAILED", "rejected"),
        ("REJECTED", "rejected"),
        ("FAILED", "rejected"),
    ],
)
async def test_campaign_status_mapping(campaign_status, state) -> None:
    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "VERIFIED"})
        if req.url.path.endswith("/10dlc/campaign/C1"):
            return httpx.Response(
                200, json={"campaignStatus": campaign_status, "failureReasons": None}
            )
        return httpx.Response(404)

    refs = {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "campaign_id": "C1",
        "subject_type": "business",
        "campaign": {},
    }
    assert (await _provider(handler).status(refs)).state == state


PERSON_FIELDS = {
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "mobile_phone_number": "+15125550100",
    "campaign_description": "Appointment reminders for my own clients who asked for them.",
    "sample_1": "Ada: your session is tomorrow at 10. Reply STOP to opt out.",
    "sample_2": "Ada: thanks for booking. Reply STOP to opt out.",
    "opt_in_description": "Clients give their number on my booking form.",
    "has_links": "NO",
    "privacy_policy_url": "https://ada.example/privacy",
    "terms_url": "https://ada.example/terms",
}


async def test_person_requirements_offer_both_subject_types() -> None:
    p = _provider(lambda req: httpx.Response(200, json={}))
    req = await p.requirements("US", "local", "person")
    assert req.subject_type == "person"
    assert req.subject_types == ("person", "business")
    assert {f.name for f in req.fields} >= {"first_name", "mobile_phone_number"}
    business = await p.requirements("US", "local", "business")
    assert business.subject_types == ("person", "business")


async def test_person_draft_creates_sole_proprietor_brand_and_sends_pin() -> None:
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.url.path.endswith("/smsOtp"):
            return httpx.Response(200, json={"brandId": "B1", "referenceId": "R1"})
        return httpx.Response(404)

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems == []
    assert [c[:2] for c in calls] == [
        ("POST", "/v2/10dlc/brand"),
        ("POST", "/v2/10dlc/brand/B1/smsOtp"),
    ]
    body = calls[0][2]
    assert body["entityType"] == "SOLE_PROPRIETOR"
    assert (body["firstName"], body["lastName"]) == ("Ada", "Lovelace")
    assert body["mobilePhone"] == "+15125550100"
    assert body["displayName"] == "Ada Lovelace" == body["companyName"]
    assert body["email"] == "ada@example.com" and body["vertical"] == "COMMUNICATION"
    assert "ein" not in body and "website" not in body
    otp = calls[1][2]
    assert "@OTP_PIN@" in otp["pinSms"] and otp["pinSms"].startswith("Hail:")
    assert otp["successSms"] == "Hail: your number is confirmed. Reply STOP to opt out."
    refs = draft.refs
    assert refs["brand_id"] == "B1" and refs["otp_reference_id"] == "R1"
    assert refs["subject_type"] == "person" and refs["confirm_attempts"] == 0
    datetime.fromisoformat(refs["otp_sent_at"])
    assert "Ada" not in repr({k: v for k, v in refs.items() if k != "campaign"})
    assert "5550100" not in repr(refs)


async def test_person_mobile_must_be_us_or_canadian() -> None:
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    draft = await _draft(
        _provider(handler),
        subject="person",
        fields={**PERSON_FIELDS, "mobile_phone_number": "+447700900123"},
    )
    assert draft.problems == [
        Problem(
            field="mobile_phone_number", message="Use a US or Canadian mobile number."
        )
    ]
    assert calls == []


async def test_person_missing_fields_are_problems() -> None:
    draft = await _draft(
        _provider(lambda req: httpx.Response(200, json={})),
        subject="person",
        fields={**PERSON_FIELDS, "first_name": "", "email": ""},
    )
    assert {p.field for p in draft.problems} == {"first_name", "email"}


def _confirm_provider(code: int, seen: list):
    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        return httpx.Response(code)

    return _provider(handler)


async def test_confirm_puts_the_pin() -> None:
    seen: list = []
    p = _confirm_provider(204, seen)
    assert await p.confirm({"brand_id": "B1"}, "123456") == []
    assert seen == [("PUT", "/v2/10dlc/brand/B1/smsOtp", {"otpPin": "123456"})]


async def test_confirm_wrong_code_is_a_problem() -> None:
    p = _confirm_provider(400, [])
    problems = await p.confirm({"brand_id": "B1"}, "000000")
    assert [pr.field for pr in problems] == ["code"]
    assert "expired" in problems[0].message


async def test_confirm_server_error_raises() -> None:
    p = _confirm_provider(500, [])
    with pytest.raises(VerificationProviderError):
        await p.confirm({"brand_id": "B1"}, "123456")


async def test_confirmation_is_code_until_identity_verified() -> None:
    p = _provider(lambda req: httpx.Response(200, json={}))
    refs = {"subject_type": "person", "brand_id": "B1"}
    assert p.confirmation(refs) == "code"
    refs["identity_verified"] = True
    assert p.confirmation(refs) is None
    assert p.confirmation({"subject_type": "business"}) is None


def _person_refs(**extra) -> dict:
    return {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "person",
        "campaign": {
            "use_case": "SOLE_PROPRIETOR",
            "description": "d" * 40,
            "message_flow": "f" * 40,
            "samples": ["s" * 20, "t" * 20],
            "has_links": False,
            "privacy_policy_url": "https://acme.example/privacy",
            "terms_url": "https://acme.example/terms",
        },
        **extra,
    }


async def test_status_marks_identity_verified_and_creates_campaign() -> None:
    posted = []

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "VERIFIED"})
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    refs = _person_refs(otp_sent_at=datetime.now(timezone.utc).isoformat())
    st = await _provider(handler).status(refs)
    assert st.state == "pending"
    assert refs["identity_verified"] is True and refs["campaign_id"] == "C1"
    assert posted[0]["usecase"] == "SOLE_PROPRIETOR"


def _unverified_handler(otp_posts: list):
    def handler(req):
        if req.url.path.endswith("/smsOtp"):
            otp_posts.append((req.method, json.loads(req.content)))
            return httpx.Response(200, json={"brandId": "B1", "referenceId": "R2"})
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "UNVERIFIED"})
        return httpx.Response(404)

    return handler


async def test_status_resends_pin_after_a_day() -> None:
    otp_posts: list = []
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    refs = _person_refs(otp_sent_at=old, otp_reference_id="R1")
    st = await _provider(_unverified_handler(otp_posts)).status(refs)
    assert st.state == "pending"
    assert len(otp_posts) == 1 and otp_posts[0][0] == "POST"
    assert "@OTP_PIN@" in otp_posts[0][1]["pinSms"]
    assert refs["otp_sent_at"] != old and refs["otp_reference_id"] == "R2"
    assert "identity_verified" not in refs and "campaign_id" not in refs


async def test_status_does_not_resend_a_fresh_pin() -> None:
    otp_posts: list = []
    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    refs = _person_refs(otp_sent_at=fresh)
    st = await _provider(_unverified_handler(otp_posts)).status(refs)
    assert st.state == "pending" and otp_posts == []
    assert refs["otp_sent_at"] == fresh


async def test_status_creates_campaign_after_confirm_even_if_brand_lags() -> None:
    otp_posts: list = []
    base = _unverified_handler(otp_posts)

    def handler(req):
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            return httpx.Response(200, json={"campaignId": "C1"})
        return base(req)

    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    refs = _person_refs(otp_sent_at=old, identity_verified=True)
    st = await _provider(handler).status(refs)
    assert st.state == "pending" and refs["campaign_id"] == "C1"
    assert otp_posts == []


async def test_a_new_pin_resets_the_confirm_attempts() -> None:
    otp_posts: list = []
    old = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    refs = _person_refs(otp_sent_at=old, confirm_attempts=5)
    await _provider(_unverified_handler(otp_posts)).status(refs)
    assert len(otp_posts) == 1 and refs["confirm_attempts"] == 0
    fresh = _person_refs(
        otp_sent_at=datetime.now(timezone.utc).isoformat(), confirm_attempts=3
    )
    await _provider(_unverified_handler([])).status(fresh)
    assert fresh["confirm_attempts"] == 3
