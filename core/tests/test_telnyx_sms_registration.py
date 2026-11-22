import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.core.providers.verification import telnyx_sms
from hailhq.core.providers.verification.base import (
    CAMPAIGN_REJECTED,
    Address,
    DiscardRefused,
    Problem,
    RegistrationHoldsAnotherNumber,
    VerificationProviderError,
)
from hailhq.core.providers.verification.sms_review import (
    LOW_VOLUME_FASTER_LANE_FAILED,
    campaign_repair_reason,
)
from hailhq.core.providers.verification.telnyx_sms import (
    TelnyxSmsRegistrationProvider,
    _duplicate_by_text,
    _reason_problems,
)


@pytest.fixture(autouse=True)
def _no_brand_wait(monkeypatch):
    monkeypatch.setattr(telnyx_sms, "_BRAND_READ_GAP", 0)


FIELDS = {
    "sender_country": "US",
    "has_tax_id": "YES",
    "program_name": "Acme",
    "opt_in_message": "Acme: Enrolled for appointment updates, once per appointment. Msg and data rates may apply. Reply HELP for help, STOP to stop.",
    "message_frequency": "once per appointment",
    "disclosure_purposes_confirmed": "YES",
    "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
    "sample_1_type": "CUSTOMER_CARE",
    "sample_2_type": "ACCOUNT_NOTIFICATION",
    "consent_methods": "WEBSITE",
    "consent_disclosure": "Get Acme appointment reminders, once per appointment. Message and data rates may apply. Reply STOP to opt out or HELP for help. https://acme.org/privacy https://acme.org/terms",
    "consent_records": "We privately retain the checkbox selection, timestamp and disclosure version.",
    "consent_optional": "YES",
    "marketing_consent": "NO",
    "evidence_redacted": "YES",
    "website_url": "https://acme.org/book",
    "website_evidence_url": "https://acme.org/evidence/booking.png",
    "website_action": "Select the initially unchecked SMS reminder checkbox, then submit.",
    "policies_confirmed": "YES",
    "has_phone": "NO",
    "age_gated": "NO",
    "direct_lending": "NO",
    "restricted_content": "NONE",
    "registration_country": "US",
    "legal_name": "Acme Inc",
    "business_type": "PRIVATE_PROFIT",
    "ein": "12-3456789",
    "industry": "RETAIL",
    "website": "https://acme.org",
    "rep_name": "Ada Lovelace",
    "rep_email": "ada@acme.example",
    "rep_phone": "+15125550100",
    "rep_title": "CEO",
    "daily_texts": "OVER_2000",
    "campaign_description": "Order updates and delivery notices for customers who ordered on our site.",
    "sample_1": "Acme: your order 123 shipped. Reply STOP to opt out.",
    "sample_2": "Acme: your order 123 was delivered. Reply STOP to opt out.",
    "opt_in_description": "Customers tick a box at checkout on acme.example.",
    "has_links": "NO",
    "privacy_policy_url": "https://acme.org/privacy",
    "terms_url": "https://acme.org/terms",
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
    def answer(req):
        resp = handler(req)
        # No campaign filed yet for the brand, unless the test says so.
        if (
            req.method == "GET"
            and req.url.path.endswith("/10dlc/campaign")
            and resp.status_code == 404
        ):
            return httpx.Response(200, json={"records": []})
        return resp

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(answer), base_url="https://api.telnyx.com/v2"
    )
    return TelnyxSmsRegistrationProvider(api_key="k", client=client)


async def _draft(p, subject="business", fields=FIELDS, address=ADDRESS, **kwargs):
    req = await p.requirements("US", "local", subject)
    return await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=fields,
        address=address,
        documents={},
        messaging_service_sid="MP1",
        **kwargs,
    )


async def test_swedish_company_files_its_vat_number_and_country() -> None:
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(
                200, json={"brandId": "B1", "identityStatus": "SELF_DECLARED"}
            )
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    stockholm = Address(
        customer_name="Acme AB",
        street="Drottninggatan 1",
        city="Stockholm",
        region="Stockholm",
        postal_code="111 51",
        country_code="SE",
    )
    fields = {
        **FIELDS,
        "sender_country": "SE",
        "registration_country": "SE",
        "ein": "SE 556677-8899 01",
        "rep_phone": "+46701234567",
    }
    draft = await _draft(_provider(handler), fields=fields, address=stockholm)
    assert draft.problems == []
    body = calls[0][2]
    assert body["country"] == "SE" and body["ein"] == "556677889901"


def test_a_foreign_tax_number_must_match_the_address_country() -> None:
    from hailhq.core.providers.verification.sms_review import sender_problems

    fields = {"sender_country": "US", "registration_country": "SE"}
    problems = sender_problems(fields, ADDRESS, "business")
    assert [p.field for p in problems] == ["registration_country"]
    # A Canadian number with a US address is a carrier rule, not this one.
    assert not sender_problems(
        {**fields, "registration_country": "CA"}, ADDRESS, "business"
    )


async def test_business_draft_creates_brand_and_requests_vetting() -> None:
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(
                200, json={"brandId": "B1", "identityStatus": "SELF_DECLARED"}
            )
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    draft = await _draft(_provider(handler))
    assert draft.problems == []
    assert (
        draft.refs["brand_id"] == "B1" and draft.refs["messaging_profile_id"] == "MP1"
    )
    assert draft.refs["subject_type"] == "business"
    # Personal data stays in the campaign snapshot and the sender name.
    assert draft.refs["subject_name"] == "Acme Inc"
    assert "Acme" not in repr(
        {k: v for k, v in draft.refs.items() if k not in ("campaign", "subject_name")}
    )
    method, path, body = calls[0]
    assert (method, path) == ("POST", "/v2/10dlc/brand")
    assert body["entityType"] == "PRIVATE_PROFIT" and body["ein"] == "123456789"
    assert body["country"] == "US" and body["state"] == "TX"
    assert body["website"] == "https://acme.org"
    assert calls[1][:2] == ("GET", "/v2/10dlc/brand/B1")
    assert calls[2][:2] == ("POST", "/v2/10dlc/brand/B1/externalVetting")
    assert calls[2][2] == {"evpId": "AEGIS", "vettingClass": "STANDARD"}
    assert draft.refs["vetting_requested"] is True


async def test_bad_link_is_a_problem_and_sends_nothing() -> None:
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    draft = await _draft(
        _provider(handler), fields={**FIELDS, "terms_url": "acme.example/terms"}
    )
    assert "terms_url" in [p.field for p in draft.problems]
    assert draft.refs == {} and calls == []


async def test_a_name_too_long_for_the_brand_is_a_problem_and_sends_nothing() -> None:
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, json={})

    fields = {**PERSON_FIELDS, "first_name": "a" * 50, "last_name": "b" * 50}
    draft = await _draft(_provider(handler), subject="person", fields=fields)
    assert [p.field for p in draft.problems] == ["last_name"]
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
            "schema_version": 2,
            "opt_in_message": "Acme: Actual enrollment confirmation. Reply HELP for help, STOP to stop.",
            "has_phone": False,
            "age_gated": False,
            "direct_lending": False,
            "text_types": ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"],
            "privacy_policy_url": "https://acme.org/privacy",
            "terms_url": "https://acme.org/terms",
        },
    }
    st = await p.status(refs)
    assert st.state == "pending" and refs["campaign_id"] == "C1"
    assert posted[0]["usecase"] == "MIXED" and posted[0]["brandId"] == "B1"
    assert posted[0]["sample1"] == "s" * 20 and posted[0]["embeddedLink"] is False
    assert posted[0]["optoutKeywords"] == "STOP,STOPALL,UNSUBSCRIBE,CANCEL,END,QUIT"
    assert posted[0]["privacyPolicyLink"] == "https://acme.org/privacy"
    assert posted[0]["termsAndConditionsLink"] == "https://acme.org/terms"
    # MIXED needs 2 or more sub use cases; Telnyx refuses with 400 without.
    assert posted[0]["subUsecases"] == ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"]
    assert posted[0]["termsAndConditions"] is True
    # Telnyx 10015 "optoutMessage is required if subscriberOptout is true."
    assert posted[0]["subscriberOptout"] is True
    # No name on the brand read: the replies are filed without one.
    assert posted[0]["optoutMessage"] == (
        "You are unsubscribed and will get no more messages from this workspace."
    )


def _unfiled_campaign_refs() -> dict:
    return {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "campaign": {
            "use_case": "MIXED",
            "description": "d" * 40,
            "message_flow": "f" * 40,
            "samples": ["s" * 20, "t" * 20],
            "has_links": False,
            "schema_version": 2,
            "opt_in_message": "Acme: Actual enrollment confirmation. Reply HELP for help, STOP to stop.",
            "text_types": ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"],
        },
    }


async def test_status_files_no_campaign_while_it_may_not() -> None:
    # Without credit for the campaign fee: no campaignBuilder call, the
    # brand read still runs.
    paths = []

    def handler(req):
        paths.append(req.url.path)
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"identityStatus": "VETTED_VERIFIED", "failureReasons": None}
            )
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    refs = _unfiled_campaign_refs()
    st = await _provider(handler).status(refs, may_file_campaign=False)
    assert st.state == "pending" and "campaign_id" not in refs
    assert any(p.endswith("/10dlc/brand/B1") for p in paths)
    assert not any(p.endswith("/10dlc/campaignBuilder") for p in paths)


async def test_status_applies_a_brand_rejection_while_no_campaign_may_be_filed() -> (
    None
):
    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "REGISTRATION_FAILED", "failureReasons": None}
            )
        return httpx.Response(404)

    st = await _provider(handler).status(
        _unfiled_campaign_refs(), may_file_campaign=False
    )
    assert st.state == "rejected"


def test_campaign_replies_open_with_the_brand_name() -> None:
    from hailhq.core.providers.verification.telnyx_sms import _campaign_replies

    refs = {"campaign": {"contact_email": "help@acme.example"}}
    replies = _campaign_replies({"displayName": "Acme Dental"}, refs)
    assert replies == {
        "helpMessage": (
            "Acme Dental: Reply STOP to opt out. For help contact help@acme.example."
        ),
        "optinMessage": (
            "Acme Dental: You are opted in. Msg frequency varies. "
            "Msg and data rates may apply. Reply HELP for help, STOP to opt out."
        ),
        "optoutMessage": (
            "Acme Dental: You are unsubscribed and will get no more messages "
            "from this workspace."
        ),
    }
    person = _campaign_replies({"firstName": "Jo", "lastName": "Doe"}, refs)
    assert person["optoutMessage"].startswith("Jo Doe: You are unsubscribed")


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
            "schema_version": 1,
            "has_phone": False,
            "age_gated": False,
            "direct_lending": False,
            "text_types": ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"],
        },
    }
    result = await _provider(handler).status(refs)
    assert result.state == "pending" and "consent evidence" in result.reason
    assert refs["needs_information"] is True
    assert posted == []


async def test_status_waits_while_identity_not_verified() -> None:
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(req)
        return httpx.Response(200, json={"identityStatus": "UNVERIFIED"})

    p = _provider(handler)
    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": {},
        "vetting_requested": True,
    }
    assert (await p.status(refs)).state == "pending"
    assert posted == [] and "campaign_id" not in refs


async def test_status_rejected_brand() -> None:
    p = _provider(
        lambda req: httpx.Response(
            200, json={"identityStatus": "UNVERIFIED", "failureReasons": "EIN mismatch"}
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "business", "campaign": {}})
    assert st.state == "rejected"
    assert st.reason == (
        "Check the business tax number. "
        "The carrier said: EIN mismatch. Fix that and start again."
    )
    assert "EIN" in st.raw_reason


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
        "replies_set": True,
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
    assert st.state == "rejected" and "message examples" in st.reason
    assert "opt-out" in st.raw_reason


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
        if req.method == "GET":
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(
                422,
                json={"errors": [{"detail": "Telnyx says the EIN 123456789 is bad"}]},
            )
        return httpx.Response(200, json={})

    draft = await _draft(_provider(handler))
    assert draft.refs == {}
    assert len(draft.problems) == 1
    msg = draft.problems[0].message
    # Hail's sentence first, then the carrier's own words.
    assert msg == (
        "Check the business tax number. "
        "The carrier said: Telnyx says the EIN 123456789 is bad."
    )
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


async def test_a_refused_attach_carries_telnyx_reason() -> None:
    def handler(req):
        return httpx.Response(
            422, json={"errors": [{"code": "10015", "detail": "Number not eligible"}]}
        )

    with pytest.raises(VerificationProviderError, match="Number not eligible"):
        await _provider(handler).attach_number(
            {"campaign_id": "C1"}, provider_resource_id="PN1", e164="+15125550100"
        )


def _linked_handler(
    calls: list, records: list, list_status: int = 200, list_errors=None
):
    """Telnyx with ``records`` on the campaign, served in pages as the API
    reference says (``page``, ``recordsPerPage``, ``totalRecords``); a link
    answers 200."""

    def handler(req):
        calls.append((req.method, req.url.path, dict(req.url.params)))
        if req.method == "GET":
            if list_status != 200:
                return httpx.Response(list_status, json={"errors": list_errors or []})
            page = int(req.url.params.get("page", 1))
            size = int(req.url.params.get("recordsPerPage", 20))
            return httpx.Response(
                200,
                json={
                    "records": records[(page - 1) * size : page * size],
                    "page": page,
                    "totalRecords": len(records),
                },
            )
        return httpx.Response(200, json={})

    return handler


def _link(number: str, status: str = "ASSIGNED", campaign: str = "C1") -> dict:
    return {
        "phoneNumber": number,
        "campaignId": campaign,
        "telnyxCampaignId": campaign,
        "assignmentStatus": status,
    }


def _unlisted(record: dict) -> dict:
    return {k: v for k, v in record.items() if k != "assignmentStatus"}


async def test_a_persons_campaign_with_another_number_is_refused() -> None:
    calls = []
    p = _provider(_linked_handler(calls, [_link("+15125550199")]))
    with pytest.raises(VerificationProviderError, match=r"\+15125550199"):
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert calls == [
        (
            "GET",
            "/v2/10dlc/phone_number_campaigns",
            {"filter[telnyx_campaign_id]": "C1", "recordsPerPage": "20", "page": "1"},
        )
    ]


async def test_a_persons_number_already_on_the_campaign_is_a_success() -> None:
    calls = []
    # A link that timed out went through: Telnyx has it linked.
    p = _provider(_linked_handler(calls, [_link("+15125550100", "ASSIGNED")]))
    await p.attach_number(
        _person_refs(campaign_id="C1"),
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert [c[0] for c in calls] == ["GET"]


@pytest.mark.parametrize("link_status", [200, 409])
@pytest.mark.parametrize(
    "record",
    [
        _link("+15125550100", "PENDING_ASSIGNMENT"),
        _link("+15125550100", "FAILED_UNASSIGNMENT"),
        _link("+15125550100", None),
        _unlisted(_link("+15125550100")),
        _link("+15125550100", "SOMETHING_NEW"),
    ],
    ids=["pending", "failed_unlink", "null", "missing", "unknown"],
)
async def test_a_persons_number_not_yet_linked_is_linked_again(
    record, link_status
) -> None:
    # Only ASSIGNED is a done link. Any other status of the number tried
    # sends the link, so Hail never records a link Telnyx may not finish; a
    # 409 on it is a success.
    calls = []
    handler = _linked_handler(calls, [record])

    def answer(req):
        response = handler(req)
        return response if req.method == "GET" else httpx.Response(link_status)

    await _provider(answer).attach_number(
        _person_refs(campaign_id="C1"),
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert [c[0] for c in calls] == ["GET", "POST"]


async def test_a_persons_pending_number_still_meets_another_linked_one() -> None:
    # The number tried is pending and another number is linked: the
    # other-number rule applies, naming only the other number.
    calls = []
    records = [_link("+15125550100", "PENDING_ASSIGNMENT"), _link("+15125550199")]
    p = _provider(_linked_handler(calls, records))
    with pytest.raises(RegistrationHoldsAnotherNumber) as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert caught.value.e164 == "+15125550199"
    assert "+15125550100" not in str(caught.value)
    assert [c[0] for c in calls] == ["GET"]


@pytest.mark.parametrize(
    "records",
    [
        [],
        # A failed link and a number being unlinked do not hold the campaign.
        [_link("+15125550199", "FAILED_ASSIGNMENT")],
        [_link("+15125550199", "PENDING_UNASSIGNMENT")],
        # Another campaign's number, in case Telnyx ignores the filter.
        [_link("+15125550199", campaign="C2")],
    ],
)
async def test_a_persons_free_campaign_gets_the_number(records) -> None:
    calls = []
    p = _provider(_linked_handler(calls, records))
    await p.attach_number(
        _person_refs(campaign_id="C1"),
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert [c[:2] for c in calls] == [
        ("GET", "/v2/10dlc/phone_number_campaigns"),
        ("POST", "/v2/10dlc/phone_number_campaigns"),
    ]


async def test_a_linked_number_on_the_campaign_can_be_recorded() -> None:
    calls = []
    p = _provider(_linked_handler(calls, [_link("+15125550199", "ASSIGNED")]))
    with pytest.raises(RegistrationHoldsAnotherNumber) as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert caught.value.e164 == "+15125550199"
    assert "+15125550199 (ASSIGNED)" in str(caught.value)
    assert [c[0] for c in calls] == ["GET"]


@pytest.mark.parametrize(
    "record",
    [
        _link("+15125550199", None),
        _unlisted(_link("+15125550199")),
        _link("+15125550199", "SOMETHING_NEW"),
    ],
    ids=["null", "missing", "unknown"],
)
async def test_a_number_with_no_known_status_holds_the_campaign(record) -> None:
    # Fail safe: only a failed link and a number being unlinked free it.
    calls = []
    p = _provider(_linked_handler(calls, [record]))
    with pytest.raises(VerificationProviderError, match=r"\+15125550199") as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert not isinstance(caught.value, RegistrationHoldsAnotherNumber)
    assert [c[0] for c in calls] == ["GET"]


@pytest.mark.parametrize("status", ["PENDING_ASSIGNMENT", "FAILED_UNASSIGNMENT"])
async def test_a_number_not_yet_linked_is_named_with_its_status(status) -> None:
    # Still linking or unlinking: the number tried is refused, Hail records
    # nothing, and the worker tries again after the wait.
    calls = []
    p = _provider(_linked_handler(calls, [_link("+15125550199", status)]))
    with pytest.raises(VerificationProviderError) as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert not isinstance(caught.value, RegistrationHoldsAnotherNumber)
    assert f"+15125550199 ({status})" in str(caught.value)
    assert [c[0] for c in calls] == ["GET"]


async def test_a_persons_number_being_unlinked_is_linked_again() -> None:
    # Telnyx is about to unlink it: not a success, so the link is sent.
    calls = []
    p = _provider(
        _linked_handler(calls, [_link("+15125550100", "PENDING_UNASSIGNMENT")])
    )
    await p.attach_number(
        _person_refs(campaign_id="C1"),
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert [c[0] for c in calls] == ["GET", "POST"]


async def test_two_numbers_on_a_persons_campaign_name_no_one_number() -> None:
    calls = []
    records = [_link("+15125550198"), _link("+15125550199")]
    p = _provider(_linked_handler(calls, records))
    with pytest.raises(
        VerificationProviderError,
        match=r"5550198 \(ASSIGNED\), \+15125550199 \(ASSIGNED\)",
    ) as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert not isinstance(caught.value, RegistrationHoldsAnotherNumber)


async def test_the_campaign_check_reads_every_page() -> None:
    # Newest first: failed links fill page 1, the number that holds the
    # campaign is on page 2.
    calls = []
    records = [_link(f"+1512555{i:04}", "FAILED_ASSIGNMENT") for i in range(20)]
    records.append(_link("+15125550199"))
    p = _provider(_linked_handler(calls, records))
    with pytest.raises(RegistrationHoldsAnotherNumber) as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert caught.value.e164 == "+15125550199"
    assert [(c[0], c[2].get("page")) for c in calls] == [("GET", "1"), ("GET", "2")]


async def test_the_campaign_check_stops_at_the_last_page() -> None:
    calls = []
    records = [_link(f"+1512555{i:04}", "FAILED_ASSIGNMENT") for i in range(20)]
    p = _provider(_linked_handler(calls, records))
    await p.attach_number(
        _person_refs(campaign_id="C1"),
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert [(c[0], c[2].get("page")) for c in calls] == [("GET", "1"), ("POST", None)]


async def test_a_failed_campaign_check_keeps_telnyx_reason() -> None:
    calls = []
    errors = [{"code": "10015", "title": "Bad Request", "detail": "Invalid filter"}]
    p = _provider(_linked_handler(calls, [], list_status=400, list_errors=errors))
    with pytest.raises(VerificationProviderError) as caught:
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert str(caught.value).startswith("status 400: ")
    assert "10015" in str(caught.value) and "Invalid filter" in str(caught.value)
    assert [c[0] for c in calls] == ["GET"]


async def test_the_campaign_check_stops_when_telnyx_repeats_a_page() -> None:
    # Telnyx ignores ``page``: page 2 is page 1 again, so the holder on the
    # real page 2 is never seen. No link is sent.
    calls = []
    records = [_link(f"+1512555{i:04}", "FAILED_ASSIGNMENT") for i in range(20)]

    def handler(req):
        calls.append((req.method, req.url.params.get("page")))
        if req.method == "GET":
            return httpx.Response(
                200, json={"records": records, "page": 1, "totalRecords": 21}
            )
        return httpx.Response(200, json={})

    with pytest.raises(VerificationProviderError, match="repeats an earlier page"):
        await _provider(handler).attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert calls == [("GET", "1"), ("GET", "2")]


async def test_a_persons_link_is_not_sent_when_telnyx_cannot_be_read() -> None:
    calls = []
    p = _provider(_linked_handler(calls, [], list_status=500))
    with pytest.raises(VerificationProviderError):
        await p.attach_number(
            _person_refs(campaign_id="C1"),
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert [c[0] for c in calls] == ["GET"]


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


async def test_detach_done_on_a_2xx_with_an_unreadable_body() -> None:
    """Telnyx answered 2xx: the link is gone, even if the body does not parse."""

    def handler(req):
        return httpx.Response(200, content=b"<html>")

    assert (
        await _provider(handler).detach_number(
            {}, provider_resource_id="PN1", e164="+15125550100"
        )
        is False
    )


@pytest.mark.parametrize("status", [400, 403, 500])
async def test_detach_raises_when_the_link_stays(status) -> None:
    """The cancel counts the error (refusal or outage); a 404 is done."""

    def handler(req):
        return httpx.Response(status, json={})

    with pytest.raises(httpx.HTTPStatusError):
        await _provider(handler).detach_number(
            {}, provider_resource_id="PN1", e164="+15125550100"
        )


async def test_detach_raises_on_a_lost_connection() -> None:
    def handler(req):
        raise httpx.ConnectError("x", request=req)

    with pytest.raises(httpx.ConnectError):
        await _provider(handler).detach_number(
            {}, provider_resource_id="PN1", e164="+15125550100"
        )


async def test_discard_order_and_raises_after_every_step_on_an_outage() -> None:
    """A failed campaign delete is not lost: the caller keeps the ids and
    tries again. 404 (already gone) counts as done; a refusal raises
    ``DiscardRefused``; a 429 is an outage."""
    calls = []
    codes = {"campaign": 500}

    def handler(req):
        calls.append((req.method, req.url.path))
        if "/campaign/" in req.url.path:
            return httpx.Response(codes["campaign"], json={})
        return httpx.Response(200, json={})

    p = _provider(handler)
    with pytest.raises(VerificationProviderError):
        await p.discard({"brand_id": "B1", "campaign_id": "C1"})
    assert calls == [
        ("DELETE", "/v2/10dlc/campaign/C1"),
        ("DELETE", "/v2/10dlc/brand/B1"),
    ]
    codes["campaign"] = 404
    await p.discard({"brand_id": "B1", "campaign_id": "C1"})
    for status in (403, 409, 422):
        codes["campaign"] = status
        with pytest.raises(DiscardRefused):
            await p.discard({"brand_id": "B1", "campaign_id": "C1"})
    codes["campaign"] = 429
    with pytest.raises(VerificationProviderError) as raised:
        await p.discard({"campaign_id": "C1"})
    assert not isinstance(raised.value, DiscardRefused)
    await p.discard({})


async def test_discard_raises_on_a_timeout() -> None:
    def handler(req):
        raise httpx.ReadTimeout("slow", request=req)

    with pytest.raises(VerificationProviderError) as raised:
        await _provider(handler).discard({"campaign_id": "C1"})
    assert not isinstance(raised.value, DiscardRefused)


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
        "replies_set": True,
    }
    assert (await _provider(handler).status(refs)).state == state


PERSON_FIELDS = {
    "sender_country": "US",
    "has_tax_id": "NO",
    "program_name": "Ada",
    "opt_in_message": "Ada: Enrolled for appointment updates, once per appointment. Msg and data rates may apply. Reply HELP for help, STOP to stop.",
    "message_frequency": "once per appointment",
    "disclosure_purposes_confirmed": "YES",
    "text_types": "CUSTOMER_CARE,ACCOUNT_NOTIFICATION",
    "sample_1_type": "CUSTOMER_CARE",
    "sample_2_type": "ACCOUNT_NOTIFICATION",
    "consent_methods": "WEBSITE",
    "consent_disclosure": "Get Ada appointment reminders, once per appointment. Message and data rates may apply. Reply STOP to opt out or HELP for help. https://ada.org/privacy https://ada.org/terms",
    "consent_records": "We privately retain the checkbox selection, timestamp and disclosure version.",
    "consent_optional": "YES",
    "marketing_consent": "NO",
    "evidence_redacted": "YES",
    "website_url": "https://acme.org/book",
    "website_evidence_url": "https://acme.org/evidence/booking.png",
    "website_action": "Select the initially unchecked SMS reminder checkbox, then submit.",
    "policies_confirmed": "YES",
    "has_phone": "NO",
    "age_gated": "NO",
    "direct_lending": "NO",
    "restricted_content": "NONE",
    "registration_country": "US",
    "first_name": "Ada",
    "last_name": "Lovelace",
    "email": "ada@example.com",
    "mobile_phone_number": "+15125550100",
    "campaign_description": "Appointment reminders for my own clients who asked for them.",
    "sample_1": "Ada: your session is tomorrow at 10. Reply STOP to opt out.",
    "sample_2": "Ada: thanks for booking. Reply STOP to opt out.",
    "opt_in_description": "Clients give their number on my booking form.",
    "has_links": "NO",
    "privacy_policy_url": "https://ada.org/privacy",
    "terms_url": "https://ada.org/terms",
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
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/smsOtp"):
            return httpx.Response(200, json={"brandId": "B1", "referenceId": "R1"})
        return httpx.Response(404)

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems == []
    assert [c[:2] for c in calls] == [
        ("POST", "/v2/10dlc/brand"),
        ("GET", "/v2/10dlc/brand/B1"),
        ("POST", "/v2/10dlc/brand/B1/smsOtp"),
    ]
    body = calls[0][2]
    assert body["entityType"] == "SOLE_PROPRIETOR"
    assert (body["firstName"], body["lastName"]) == ("Ada", "Lovelace")
    assert body["mobilePhone"] == "+15125550100"
    assert body["displayName"] == "Ada Lovelace" == body["companyName"]
    assert body["email"] == "ada@example.com" and body["vertical"] == "COMMUNICATION"
    assert "ein" not in body and "website" not in body
    otp = calls[2][2]
    assert "@OTP_PIN@" in otp["pinSms"] and otp["pinSms"].startswith("Hail:")
    assert otp["successSms"] == "Hail: your number is confirmed. Reply STOP to opt out."
    refs = draft.refs
    assert refs["brand_id"] == "B1" and refs["otp_reference_id"] == "R1"
    assert refs["subject_type"] == "person" and refs["confirm_attempts"] == 0
    datetime.fromisoformat(refs["otp_sent_at"])
    assert refs["subject_name"] == "Ada Lovelace"
    assert "Ada" not in repr(
        {k: v for k, v in refs.items() if k not in ("campaign", "subject_name")}
    )
    assert "5550100" not in repr(refs)


def _kept_brand_handler(calls, brand, campaigns=()):
    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B9"):
            if brand is None:
                return httpx.Response(404, json={})
            return httpx.Response(200, json=brand)
        if req.method == "DELETE":
            return httpx.Response(204)
        if req.url.path.endswith("/10dlc/campaign"):
            return httpx.Response(200, json={"records": list(campaigns)})
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/smsOtp"):
            return httpx.Response(200, json={"brandId": "B1", "referenceId": "R1"})
        return httpx.Response(404)

    return handler


KEPT_BRAND = {
    "status": "OK",
    "identityStatus": "VERIFIED",
    "firstName": "Ada",
    "lastName": "Lovelace",
    "email": "ADA@example.com",
    "mobilePhone": "+15125550100",
}


async def _kept_draft(p, **kwargs):
    req = await p.requirements("US", "local", "person")
    return await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=PERSON_FIELDS,
        address=ADDRESS,
        documents={},
        kept_brand_id="B9",
        **kwargs,
    )


async def test_person_reuses_the_kept_brand_with_no_pin() -> None:
    calls: list = []
    p = _provider(_kept_brand_handler(calls, KEPT_BRAND))
    draft = await _kept_draft(p)
    assert draft.problems == []
    assert [c for c in calls if "/10dlc/brand" in c[1]] == [
        ("GET", "/v2/10dlc/brand/B9")
    ]
    refs = draft.refs
    assert refs["brand_id"] == "B9" and refs["brand_reused"] is True
    assert refs["identity_verified"] is True and "otp_sent_at" not in refs
    assert p.confirmation(refs) is None


@pytest.mark.parametrize(
    "brand",
    [
        None,
        {**KEPT_BRAND, "mobilePhone": "+15125550199"},
        {**KEPT_BRAND, "lastName": "Byron"},
        {**KEPT_BRAND, "identityStatus": "UNVERIFIED"},
        {**KEPT_BRAND, "status": "REGISTRATION_FAILED"},
    ],
)
async def test_a_kept_brand_that_does_not_fit_files_a_new_one(brand) -> None:
    calls: list = []
    p = _provider(_kept_brand_handler(calls, brand))
    draft = await _kept_draft(p, new_brand_funded=True)
    assert draft.refs["brand_id"] == "B1" and "brand_reused" not in draft.refs
    assert ("POST", "/v2/10dlc/brand") in calls
    assert ("POST", "/v2/10dlc/brand/B1/smsOtp") in calls
    # Telnyx refuses the delete, and a typo must not cost the brand.
    assert ("DELETE", "/v2/10dlc/brand/B9") not in calls
    # The brand read decides it: no call to list its campaigns.
    assert not [c for c in calls if c[0] == "GET" and "/10dlc/campaign" in c[1]]


@pytest.mark.parametrize("code", [404, 410])
async def test_a_kept_brand_telnyx_no_longer_holds_files_a_new_one(code) -> None:
    calls: list = []
    inner = _kept_brand_handler(calls, KEPT_BRAND)

    def handler(req: httpx.Request):
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B9"):
            calls.append((req.method, req.url.path))
            return httpx.Response(code, json={})
        return inner(req)

    draft = await _kept_draft(_provider(handler), new_brand_funded=True)
    assert draft.refs["brand_id"] == "B1" and "brand_reused" not in draft.refs


@pytest.mark.parametrize("code", [400, 401, 403])
async def test_a_kept_brand_lookup_refusal_buys_no_new_brand(code) -> None:
    from hailhq.core.providers.verification.base import VerificationProviderError

    calls: list = []
    inner = _kept_brand_handler(calls, KEPT_BRAND)

    def handler(req: httpx.Request):
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B9"):
            calls.append((req.method, req.url.path))
            return httpx.Response(code, json={})
        return inner(req)

    with pytest.raises(VerificationProviderError):
        await _kept_draft(_provider(handler), new_brand_funded=True)
    assert ("POST", "/v2/10dlc/brand") not in calls


async def test_a_reused_brand_files_when_a_new_brand_is_not_funded() -> None:
    calls: list = []
    p = _provider(_kept_brand_handler(calls, KEPT_BRAND))
    draft = await _kept_draft(p, new_brand_funded=False)
    assert draft.needs_new_brand_credit is False
    assert draft.refs["brand_id"] == "B9" and draft.refs["brand_reused"] is True


@pytest.mark.parametrize("brand", [{**KEPT_BRAND, "lastName": "Byron"}, None])
async def test_a_brand_that_cannot_be_reused_without_credit_files_no_brand(
    brand,
) -> None:
    calls: list = []
    p = _provider(_kept_brand_handler(calls, brand))
    draft = await _kept_draft(p, new_brand_funded=False)
    assert draft.needs_new_brand_credit is True
    assert draft.refs == {} and draft.problems == [] and draft.billed_refs == {}
    assert ("POST", "/v2/10dlc/brand") not in calls


async def test_a_caller_that_does_not_say_files_no_new_brand() -> None:
    calls: list = []
    brand = {**KEPT_BRAND, "lastName": "Byron"}
    draft = await _kept_draft(_provider(_kept_brand_handler(calls, brand)))
    assert draft.needs_new_brand_credit is True
    assert ("POST", "/v2/10dlc/brand") not in calls


async def test_changed_details_with_credit_file_a_new_brand() -> None:
    calls: list = []
    brand = {**KEPT_BRAND, "lastName": "Byron"}
    p = _provider(_kept_brand_handler(calls, brand))
    draft = await _kept_draft(p, new_brand_funded=True)
    assert draft.needs_new_brand_credit is False
    assert draft.refs["brand_id"] == "B1" and "brand_reused" not in draft.refs
    assert ("POST", "/v2/10dlc/brand") in calls


async def test_a_kept_brand_matches_a_mobile_in_any_format() -> None:
    calls: list = []
    brand = {**KEPT_BRAND, "mobilePhone": "+1 512 555 0100"}
    draft = await _kept_draft(_provider(_kept_brand_handler(calls, brand)))
    assert draft.refs["brand_id"] == "B9" and draft.refs["brand_reused"] is True


async def _lookup_answering(status: int):
    calls: list = []

    def handler(req: httpx.Request):
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B9"):
            calls.append(("GET", req.url.path))
            return httpx.Response(status, json={})
        return _kept_brand_handler(calls, KEPT_BRAND)(req)

    return await _kept_draft(_provider(handler), new_brand_funded=True)


async def test_a_refused_lookup_files_a_new_brand() -> None:
    draft = await _lookup_answering(404)
    assert draft.problems == []
    assert draft.refs["brand_id"] == "B1" and "brand_reused" not in draft.refs


@pytest.mark.parametrize("status", [429, 500])
async def test_a_failed_lookup_does_not_pay_for_a_new_brand(status) -> None:
    with pytest.raises(VerificationProviderError):
        await _lookup_answering(status)


async def test_a_kept_brand_with_a_campaign_is_left_alone() -> None:
    calls: list = []
    handler = _kept_brand_handler(calls, KEPT_BRAND, [{"campaignId": "OLD"}])
    draft = await _kept_draft(_provider(handler), new_brand_funded=True)
    assert draft.refs["brand_id"] == "B1" and "brand_reused" not in draft.refs
    assert ("DELETE", "/v2/10dlc/brand/B9") not in calls


async def test_a_brand_that_fails_after_its_campaign_is_not_kept() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"failureReasons": "Identity mismatch"})

    p = _provider(handler)
    refs = {
        "brand_id": "B1",
        "campaign_id": "C1",
        "subject_type": "person",
        "identity_verified": True,
    }
    assert "Identity mismatch" in (await p.status(refs)).reason
    calls.clear()
    assert await p.discard_rejected(refs) == {}
    assert ("DELETE", "/v2/10dlc/brand/B1") in calls


async def test_discard_rejected_keeps_a_confirmed_persons_brand() -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    p = _provider(handler)
    kept = await p.discard_rejected(
        {
            "brand_id": "B1",
            "campaign_id": "C1",
            "subject_type": "person",
            "identity_verified": True,
        }
    )
    assert kept == {"kept_brand_id": "B1"}
    assert calls == [("DELETE", "/v2/10dlc/campaign/C1")]


@pytest.mark.parametrize(
    "refs",
    [
        {"subject_type": "business", "identity_verified": True},
        {"subject_type": "person"},
        # No campaign: the brand itself failed.
        {"subject_type": "person", "identity_verified": True},
    ],
)
async def test_discard_rejected_deletes_any_other_brand(refs) -> None:
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={})

    p = _provider(handler)
    assert await p.discard_rejected({"brand_id": "B1", **refs}) == {}
    assert calls == [("DELETE", "/v2/10dlc/brand/B1")]


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
            field="mobile_phone_number",
            message="Use your own US or Canadian mobile number, not a Hail or other internet phone number.",
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
    # No PIN sent yet: nothing to type.
    assert p.confirmation(refs) is None
    refs["otp_sent_at"] = datetime.now(timezone.utc).isoformat()
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
            "schema_version": 2,
            "opt_in_message": "Acme: Actual enrollment confirmation. Reply HELP for help, STOP to stop.",
            "has_phone": False,
            "age_gated": False,
            "direct_lending": False,
            "text_types": ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"],
            "privacy_policy_url": "https://acme.org/privacy",
            "terms_url": "https://acme.org/terms",
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
    # SOLE_PROPRIETOR needs 1 or more sub use cases.
    assert posted[0]["subUsecases"] == ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"]


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


async def test_pin_waits_for_the_brand_to_register() -> None:
    """Telnyx answers 404 for a brand it is still registering."""
    calls = []
    reads = iter([404, 404, 200])

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(next(reads), json={"status": "OK"})
        if req.url.path.endswith("/smsOtp"):
            return httpx.Response(200, json={"referenceId": "R1"})
        return httpx.Response(404)

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems == [] and draft.refs["otp_reference_id"] == "R1"
    assert calls.count(("GET", "/v2/10dlc/brand/B1")) == 3
    assert calls[-1] == ("POST", "/v2/10dlc/brand/B1/smsOtp")


async def test_refused_brand_names_the_fields_and_is_deleted() -> None:
    calls = []
    reasons = [
        {"fields": ["phone"], "description": "Phone duplicate threshold reached"},
        {"fields": ["email"], "description": "Email duplicate threshold reached"},
        {"fields": ["state"], "description": "Invalid state"},
    ]

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            return httpx.Response(
                200, json={"status": "REGISTRATION_FAILED", "failureReasons": reasons}
            )
        return httpx.Response(204)

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.refs == {}
    assert draft.billed_refs == {"brand_id": "B1", "subject_type": "person"}
    by_field = {p.field: p for p in draft.problems}
    assert set(by_field) == {"mobile_phone_number", "email", "address.region"}
    assert "too many registrations" in by_field["mobile_phone_number"].message
    assert "too many registrations" in by_field["email"].message
    assert (
        by_field["address.region"].message
        == "The 10DLC registry did not accept this value. "
        "The carrier said: Invalid state."
    )
    assert by_field["email"].raw == "Email duplicate threshold reached"
    assert not any(p.endswith("/smsOtp") for _, p in calls)
    assert ("DELETE", "/v2/10dlc/brand/B1") in calls


async def test_brand_still_registering_is_kept_and_status_sends_the_pin() -> None:
    calls = []
    registered = False

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            if not registered:
                return httpx.Response(404)
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/smsOtp"):
            return httpx.Response(200, json={"referenceId": "R1"})
        return httpx.Response(400)

    p = _provider(handler)
    draft = await _draft(p, subject="person", fields=PERSON_FIELDS)
    assert draft.problems == [] and draft.refs["brand_id"] == "B1"
    assert "otp_sent_at" not in draft.refs
    assert not any(m == "DELETE" for m, _ in calls)
    registered = True
    assert (await p.status(draft.refs)).state == "pending"
    assert draft.refs["otp_reference_id"] == "R1"


async def test_status_asks_for_vetting_once_if_the_draft_could_not() -> None:
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(req.url.path)
        return httpx.Response(200, json={"identityStatus": "SELF_DECLARED"})

    p = _provider(handler)
    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": {},
        "vetting_requested": False,
    }
    assert (await p.status(refs)).state == "pending"
    assert (await p.status(refs)).state == "pending"
    assert posted == ["/v2/10dlc/brand/B1/externalVetting"]
    assert refs["vetting_requested"] is True


async def test_status_never_asks_again_for_a_row_filed_before_the_flag() -> None:
    """Rows filed before ``vetting_requested`` existed already asked."""
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(req.url.path)
        return httpx.Response(200, json={"identityStatus": "SELF_DECLARED"})

    refs = {"brand_id": "B1", "subject_type": "business", "campaign": {}}
    assert (await _provider(handler).status(refs)).state == "pending"
    assert posted == [] and "vetting_requested" not in refs


async def test_business_brand_still_registering_marks_vetting_as_owed() -> None:
    def handler(req: httpx.Request):
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            return httpx.Response(404)
        return httpx.Response(400)

    draft = await _draft(_provider(handler))
    assert draft.problems == [] and draft.refs["vetting_requested"] is False


async def test_status_waits_while_the_brand_is_registering() -> None:
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(req.url.path)
        return httpx.Response(
            200,
            json={"status": "REGISTRATION_PENDING", "identityStatus": "UNVERIFIED"},
        )

    refs = {"brand_id": "B1", "subject_type": "person", "campaign": {}}
    assert (await _provider(handler).status(refs)).state == "pending"
    assert posted == [] and "otp_sent_at" not in refs


async def test_status_rejects_a_failed_brand_without_reasons() -> None:
    def handler(req):
        return httpx.Response(200, json={"status": "REGISTRATION_FAILED"})

    refs = {"brand_id": "B1", "subject_type": "person", "campaign": {}}
    assert (await _provider(handler).status(refs)).state == "rejected"


async def test_a_failed_read_keeps_waiting_instead_of_deleting_the_brand() -> None:
    calls = []
    reads = iter([503, 200])

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            return httpx.Response(next(reads), json={"status": "OK"})
        if req.url.path.endswith("/smsOtp"):
            return httpx.Response(200, json={"referenceId": "R1"})
        return httpx.Response(404)

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems == [] and draft.refs["otp_reference_id"] == "R1"
    assert not any(m == "DELETE" for m, _ in calls)


async def test_unknown_error_body_is_retryable():
    provider = _provider(lambda req: httpx.Response(400, json={"errors": 5}))
    with pytest.raises(VerificationProviderError):
        await _draft(provider, "person", PERSON_FIELDS)


async def test_4xx_pointing_at_a_field_names_that_field() -> None:
    def handler(req):
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "code": "10015",
                            "detail": "Invalid email ada@example.com",
                            "source": {"pointer": "/email"},
                        }
                    ]
                },
            )
        return httpx.Response(204)

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert [(p.field, p.message) for p in draft.problems] == [
        (
            "email",
            (
                "The 10DLC registry did not accept this value. "
                "The carrier said: Invalid email ada@example.com."
            ),
        )
    ]


def test_two_brand_reasons_on_one_field_keep_both_texts():

    problems = _reason_problems(
        [
            {"fields": ["email"], "description": "Email duplicate threshold reached"},
            {"fields": ["email"], "description": "Email domain invalid"},
        ],
        "person",
    )
    # Both texts stay on the field they name.
    assert [p.field for p in problems] == ["email"]
    assert problems[0].message == (
        "This email is on too many registrations. Use another one. The carrier "
        "said: Email duplicate threshold reached. Email domain invalid."
    )


def test_a_later_reused_email_reason_switches_the_fix():

    problems = _reason_problems(
        [
            {"fields": ["email"], "description": "[email] - Email domain invalid"},
            {"fields": ["email"], "description": "Email duplicate threshold reached"},
            {"fields": [], "description": "Vertical not allowed"},
        ],
        "person",
    )
    assert [p.field for p in problems] == ["email", ""]
    assert problems[0].message == (
        "This email is on too many registrations. Use another one. The carrier "
        "said: Email domain invalid. Email duplicate threshold reached."
    )
    # The audit row keeps the text of a reason on no form field too.
    assert problems[1].raw == "Vertical not allowed"


async def test_brand_refused_later_gets_the_reused_email_fix() -> None:
    p = _provider(
        lambda req: httpx.Response(
            200,
            json={
                "status": "REGISTRATION_FAILED",
                "failureReasons": [
                    {"fields": ["email"], "description": "Email duplicate reached"}
                ],
            },
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "person", "campaign": {}})
    assert st.reason == (
        "This email is on too many registrations. Use another one. "
        "The carrier said: Email duplicate reached. Fix that and start again."
    )


def test_reasons_under_other_text_keys_are_read():

    reason = campaign_repair_reason([{"reason": "Privacy policy missing"}])
    assert reason.startswith("Review your privacy policy")
    assert reason.endswith("The carrier said: Privacy policy missing.")


def test_same_field_reason_with_a_semicolon_is_quoted_once():

    text = "Invalid email; domain unknown"
    problems = _reason_problems(
        [{"fields": ["email"], "description": text}] * 2, "person"
    )
    assert problems[0].message.count("domain unknown") == 1
    problems = _reason_problems(
        [{"fields": ["email"], "description": "Invalid email\ndomain unknown"}] * 2,
        "person",
    )
    assert problems[0].message.count("domain unknown") == 1


def test_a_single_field_name_as_a_string_is_read():
    problems = _reason_problems(
        [{"fields": "email", "description": "Email duplicate threshold reached"}],
        "person",
    )
    assert problems[0].field == "email"
    assert "too many registrations" in problems[0].message


def test_a_field_reason_under_another_text_key_is_quoted():
    problems = _reason_problems(
        [{"fields": ["email"], "reason": "Email domain invalid"}], "person"
    )
    assert problems[0].message.endswith("The carrier said: Email domain invalid.")
    assert problems[0].raw == "Email domain invalid"


async def test_an_email_refusal_off_the_form_gets_the_email_sentence():
    def handler(req):
        return httpx.Response(
            422, json={"errors": [{"detail": "Email address is invalid"}]}
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].message == (
        "Check the email address. The carrier said: Email address is invalid."
    )


async def test_one_refusal_naming_email_and_ein_gets_both_sentences():
    def handler(req):
        return httpx.Response(
            422, json={"errors": [{"detail": "Email and EIN do not match records"}]}
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].message.startswith(
        "Check the email address. Check the business tax number."
    )


@pytest.mark.parametrize(
    ("reasons", "start"),
    [
        (
            [{"fields": [], "description": "Email duplicate threshold reached"}],
            "This email is on too many registrations.",
        ),
        (
            [{"fields": ["ein"], "description": "Invalid value"}],
            "Check the business tax number.",
        ),
        (
            [{"fields": ["postalCode"], "description": "Invalid value"}],
            "Check the address.",
        ),
        (
            [{"fields": ["website"], "description": "Invalid value"}],
            "Check the website address.",
        ),
        (
            [{"fields": ["ein"], "description": "Duplicate EIN registered"}],
            "Check the business tax number.",
        ),
    ],
)
async def test_brand_refused_later_reads_text_and_fields(reasons, start) -> None:
    p = _provider(
        lambda req: httpx.Response(
            200, json={"status": "REGISTRATION_FAILED", "failureReasons": reasons}
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "business", "campaign": {}})
    assert st.reason.startswith(start)


async def test_a_form_error_saying_permission_reaches_the_customer():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {"detail": "Invalid email", "source": {"pointer": "/email"}},
                    {"description": "Opt-in permission language missing"},
                ]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert [p.field for p in draft.problems] == ["email", ""]


def test_an_odd_fields_value_does_not_crash():
    problems = _reason_problems([{"fields": 1, "description": "x"}], "person")
    assert problems[0].message.endswith("The carrier said: x.")


async def test_an_account_error_under_description_stays_an_outage():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {"detail": "Invalid email", "source": {"pointer": "/email"}},
                    {"description": "Unauthorized: profile configuration invalid"},
                ]
            },
        )

    with pytest.raises(VerificationProviderError):
        await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)


async def test_an_off_form_duplicate_error_alone_gets_the_reuse_fix():
    def handler(req):
        return httpx.Response(
            422, json={"errors": [{"detail": "Email duplicate threshold reached"}]}
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].message.startswith(
        "This email is on too many registrations."
    )


def test_a_duplicate_phone_for_a_person_names_the_mobile_number():
    assert _duplicate_by_text("Phone duplicate reached", "person") == (
        "This mobile number is on too many registrations. Use another one."
    )
    assert _duplicate_by_text("Phone duplicate reached", "business") == (
        "This phone number is on too many registrations. Use another one."
    )


async def test_a_plain_text_duplicate_after_filing_gets_the_reuse_fix() -> None:
    p = _provider(
        lambda req: httpx.Response(
            200,
            json={
                "status": "REGISTRATION_FAILED",
                "failureReasons": "Email duplicate threshold reached",
            },
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "person", "campaign": {}})
    assert st.reason.startswith("This email is on too many registrations.")


def test_an_off_form_duplicate_at_filing_gets_the_reuse_fix():
    problems = _reason_problems(
        [{"fields": [], "description": "Email duplicate threshold reached"}],
        "person",
    )
    assert problems[-1].message.startswith("This email is on too many registrations.")


async def test_brand_refused_later_gets_the_detail_fix() -> None:
    p = _provider(
        lambda req: httpx.Response(
            200,
            json={
                "status": "REGISTRATION_FAILED",
                "failureReasons": [{"fields": [], "description": "EIN does not match"}],
            },
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "business", "campaign": {}})
    assert st.reason.startswith("Check the business tax number.")


async def test_failure_reasons_sent_as_json_text_are_read() -> None:
    reasons = json.dumps([{"fields": ["email"], "description": "Email duplicate"}])
    p = _provider(
        lambda req: httpx.Response(
            200, json={"status": "REGISTRATION_FAILED", "failureReasons": reasons}
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "person", "campaign": {}})
    assert "{" not in st.reason
    assert st.reason.startswith("This email is on too many registrations.")


async def test_one_failure_reason_object_as_json_gets_the_reuse_fix() -> None:
    reasons = json.dumps({"fields": ["email"], "description": "Email duplicate"})
    p = _provider(
        lambda req: httpx.Response(
            200, json={"status": "REGISTRATION_FAILED", "failureReasons": reasons}
        )
    )
    st = await p.status({"brand_id": "B1", "subject_type": "person", "campaign": {}})
    assert st.reason.startswith("This email is on too many registrations.")


async def test_audit_text_off_the_form_matches_the_quote():
    def handler(req):
        return httpx.Response(
            422,
            json={"errors": [{"detail": "Bad EIN", "description": "EIN mismatch"}]},
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].message.endswith("The carrier said: EIN mismatch.")
    assert draft.problems[0].raw == "EIN mismatch"


async def test_an_off_form_error_next_to_a_field_error_gets_its_fix():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {"detail": "Invalid email", "source": {"pointer": "/email"}},
                    {
                        "detail": "[ein] - EIN does not match",
                        "source": {"pointer": "/x"},
                    },
                ]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    general = draft.problems[-1]
    assert general.field == ""
    assert general.message == (
        "Check the business tax number. The carrier said: EIN does not match."
    )
    assert general.raw == "[ein] - EIN does not match"


async def test_a_field_error_under_description_is_quoted():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {
                        "description": "Email domain invalid",
                        "source": {"pointer": "/email"},
                    }
                ]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].field == "email"
    assert draft.problems[0].message.endswith("The carrier said: Email domain invalid.")


async def test_a_detail_named_only_in_the_title_gets_its_fix():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {
                        "title": "Invalid EIN",
                        "detail": "The value provided is not valid",
                    }
                ]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].message.startswith("Check the business tax number.")


async def test_a_duplicate_email_error_on_the_field_gets_the_reuse_fix():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {
                        "detail": "Email duplicate threshold reached",
                        "source": {"pointer": "/email"},
                    }
                ]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert draft.problems[0].message.startswith(
        "This email is on too many registrations."
    )


def test_a_line_break_in_a_reason_is_not_quoted():
    problems = _reason_problems(
        [{"fields": ["email"], "description": "Invalid email\ndomain unknown"}],
        "person",
    )
    assert "\n" not in problems[0].message


def test_raw_keeps_the_carriers_own_text():
    problems = _reason_problems(
        [{"fields": ["email"], "description": "[email] - Email domain invalid"}],
        "person",
    )
    assert problems[0].raw == "[email] - Email domain invalid"
    assert problems[0].message.endswith("The carrier said: Email domain invalid.")


async def test_a_non_text_detail_does_not_crash_the_filing():
    def handler(req):
        return httpx.Response(
            422,
            json={"errors": [{"detail": ["x"], "source": {"pointer": "/email"}}]},
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert [p.field for p in draft.problems] == ["email"]
    assert draft.problems[0].raw is None


def test_numeric_topic_codes_match_whole_words_only():
    assert campaign_repair_reason([{"code": "309081", "detail": "Odd"}]).startswith(
        CAMPAIGN_REJECTED
    )
    assert "privacy policy" in campaign_repair_reason([{"code": "30908"}])


def test_topic_match_reads_named_fields():

    reason = campaign_repair_reason(
        [{"fields": ["messageSamples"], "description": "No"}]
    )
    assert "message examples" in reason
    assert "message examples" in campaign_repair_reason([[{"field": "sample1"}]])


async def test_refusal_on_a_field_and_off_the_form_keeps_both_reasons():
    def handler(req):
        return httpx.Response(
            422,
            json={
                "errors": [
                    {"detail": "Invalid email", "source": {"pointer": "/email"}},
                    {"detail": "Vertical not allowed", "source": {"pointer": "/x"}},
                ]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert [(p.field, p.message) for p in draft.problems] == [
        (
            "email",
            (
                "The 10DLC registry did not accept this value. "
                "The carrier said: Invalid email."
            ),
        ),
        (
            "",
            (
                "The 10DLC registry refused a detail. Check your entries and "
                "try again. The carrier said: Vertical not allowed."
            ),
        ),
    ]


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize(
    ("pointer", "field"),
    [("/firstName", "first_name"), ("/postalCode", "address.postal_code")],
)
async def test_generic_structured_validation_returns_field_without_further_writes(
    status, pointer, field
):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(
            status,
            json={
                "errors": [{"detail": "Invalid value", "source": {"pointer": pointer}}]
            },
        )

    draft = await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert [(p.field, p.message) for p in draft.problems] == [
        (
            field,
            (
                "The 10DLC registry did not accept this value. "
                "The carrier said: Invalid value."
            ),
        )
    ]
    assert draft.refs == {} and not draft.billed_refs
    assert calls == [("POST", "/v2/10dlc/brand")]


@pytest.mark.parametrize(
    ("status", "pointer", "detail"),
    [
        (400, "/unknown", "Invalid value"),
        (422, "/messagingProfileId", "Invalid value"),
        (400, "/apiKey", "Invalid value"),
        (422, "/account/email", "Invalid value"),
        (422, "/brandId", "Invalid value"),
        (401, "/firstName", "Invalid value"),
        (403, "/postalCode", "Invalid value"),
        (400, "/firstName", "Account configuration is invalid"),
        (422, "/postalCode", "Invalid API key"),
    ],
)
async def test_operational_or_unknown_pointer_draft_failures_stay_retryable(
    status, pointer, detail
):
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(
            status,
            json={"errors": [{"detail": detail, "source": {"pointer": pointer}}]},
        )

    with pytest.raises(VerificationProviderError):
        await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)
    assert calls == [("POST", "/v2/10dlc/brand")]


async def test_status_files_the_picked_text_types() -> None:
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    p = _provider(handler)
    campaign = {
        "use_case": "MIXED",
        "description": "d",
        "message_flow": "f",
        "samples": ["s" * 20, "t" * 20],
        "has_links": False,
        "schema_version": 2,
        "opt_in_message": "Acme: Actual enrollment confirmation. Reply HELP for help, STOP to stop.",
        "has_phone": False,
        "age_gated": False,
        "direct_lending": False,
        "text_types": ["2FA", "SECURITY_ALERT"],
    }
    refs = {"brand_id": "B1", "subject_type": "business", "campaign": campaign}
    await p.status(refs)
    assert posted[0]["subUsecases"] == ["2FA", "SECURITY_ALERT"]


def test_step_follows_the_filing() -> None:
    p = _provider(lambda req: httpx.Response(200, json={}))
    person = {"subject_type": "person"}
    assert p.step(person) == "filing"  # no PIN sent yet
    assert p.step({**person, "otp_sent_at": "2026-10-28T00:00:00+00:00"}) == (
        "confirm_identity"
    )
    assert p.step({**person, "identity_verified": True}) == "filing"
    assert p.step({**person, "identity_verified": True, "campaign_id": "C1"}) == (
        "carrier_review"
    )
    assert p.step({"subject_type": "business"}) == "filing"
    assert p.step({"subject_type": "business", "vetting_requested": True}) == (
        "carrier_review"
    )


async def test_status_takes_a_campaign_filed_by_a_lost_call() -> None:
    """A campaign Telnyx made although the answer never came back is taken,
    not filed again: each review is paid."""
    posted = []

    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/10dlc/campaign"):
            assert req.url.params["brandId"] == "B1"
            return httpx.Response(200, json={"records": [{"campaignId": "C9"}]})
        if req.method == "POST":
            posted.append(req.url.path)
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    refs = {"brand_id": "B1", "subject_type": "business", "campaign": {}}
    assert (await _provider(handler).status(refs)).state == "pending"
    assert refs["campaign_id"] == "C9" and posted == []


async def test_terms_are_declared_only_with_a_terms_link() -> None:
    posted = []

    def handler(req):
        if req.method == "POST":
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    campaign = {
        "use_case": "MIXED",
        "description": "d",
        "message_flow": "f",
        "samples": ["s" * 20, "t" * 20],
        "has_links": False,
        "schema_version": 2,
        "opt_in_message": "Acme: Actual enrollment confirmation. Reply HELP for help, STOP to stop.",
        "has_phone": False,
        "age_gated": False,
        "direct_lending": False,
        "text_types": ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"],
    }
    refs = {"brand_id": "B1", "subject_type": "business", "campaign": campaign}
    await _provider(handler).status(refs)
    assert posted[0]["termsAndConditions"] is False
    assert posted[0]["subUsecases"] == ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"]


def test_error_log_names_the_field_and_reason() -> None:
    import httpx
    from hailhq.core.providers.verification.telnyx_sms import _error_log

    response = httpx.Response(
        400,
        json={
            "errors": [
                {
                    "code": "10015",
                    "title": "Bad Request",
                    "detail": "optoutMessage is required if subscriberOptout is true.",
                    "source": {"pointer": "/body/optoutMessage"},
                }
            ]
        },
    )
    assert _error_log(response) == [
        {
            "code": "10015",
            "field": "/body/optoutMessage",
            "detail": "optoutMessage is required if subscriberOptout is true.",
        }
    ]


async def test_a_failure_after_the_brand_reports_it_billed() -> None:
    """Telnyx bills a brand once made, whatever step fails after it."""

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(400, json={"errors": [{"code": "10015"}]})
        return httpx.Response(204)

    draft = await _draft(_provider(handler))
    assert not draft.problems and draft.refs["brand_id"] == "B1"
    assert draft.refs["vetting_requested"] is False


def _outage_after_the_brand(step: str, failure, calls: list):
    """Telnyx registers the brand, then ``step`` (``/externalVetting`` or
    ``/smsOtp``) fails with ``failure`` (a status code or an exception)
    until the test clears it."""
    state = {"failure": failure}

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "OK", "identityStatus": "SELF_DECLARED"}
            )
        if req.url.path.endswith(step):
            if isinstance(state["failure"], Exception):
                raise state["failure"]
            if state["failure"] == "html":
                return httpx.Response(200, content=b"<html>")
            if state["failure"] == "list":
                # JSON, but not the object the code reads.
                return httpx.Response(200, json=["odd"])
            if state["failure"]:
                return httpx.Response(state["failure"])
            return httpx.Response(200, json={"referenceId": "R1"})
        return httpx.Response(204)

    return _provider(handler), state


@pytest.mark.parametrize("failure", [502, 429, 404, httpx.ConnectError("down"), "html"])
async def test_an_outage_after_the_brand_keeps_the_business_filing(failure) -> None:
    """A retry would file and bill a new brand: keep this one, and the
    background pass asks for vetting."""
    calls: list = []
    p, state = _outage_after_the_brand("/externalVetting", failure, calls)
    draft = await _draft(p)
    assert draft.problems == [] and draft.billed_refs == {}
    assert draft.refs["brand_id"] == "B1"
    assert draft.refs["vetting_requested"] is False
    assert draft.refs["messaging_profile_id"] == "MP1" and draft.refs["campaign"]
    assert not any(m == "DELETE" for m, _ in calls)
    state["failure"] = None
    assert (await p.status(draft.refs)).state == "pending"
    assert calls[-1] == ("POST", "/v2/10dlc/brand/B1/externalVetting")
    assert draft.refs["vetting_requested"] is True


@pytest.mark.parametrize(
    "failure", [503, 404, httpx.ReadTimeout("slow"), "html", "list"]
)
async def test_an_outage_after_the_brand_keeps_the_person_filing(failure) -> None:
    calls: list = []
    p, state = _outage_after_the_brand("/smsOtp", failure, calls)
    draft = await _draft(p, subject="person", fields=PERSON_FIELDS)
    assert draft.problems == [] and draft.refs["brand_id"] == "B1"
    assert "otp_sent_at" not in draft.refs
    assert p.confirmation(draft.refs) is None
    assert not any(m == "DELETE" for m, _ in calls)
    state["failure"] = None
    assert (await p.status(draft.refs)).state == "pending"
    assert calls[-1] == ("POST", "/v2/10dlc/brand/B1/smsOtp")
    assert draft.refs["otp_reference_id"] == "R1" and draft.refs["otp_sent_at"]
    assert p.confirmation(draft.refs) == "code"


async def test_an_odd_answer_after_the_brand_keeps_the_filing() -> None:
    """Any failure after Telnyx made the brand, not only an HTTP error,
    keeps the filing: the brand is billed and never deleted."""
    calls: list = []
    p, _ = _outage_after_the_brand("/externalVetting", None, calls)

    async def broken(refs):
        raise AttributeError("odd body")

    p._request_vetting = broken
    draft = await _draft(p)
    assert draft.problems == [] and draft.billed_refs == {}
    assert draft.refs["brand_id"] == "B1"
    assert draft.refs["vetting_requested"] is False
    assert not any(m == "DELETE" for m, _ in calls)


def _vetting_retry(listed, post_status=200, post_errors=None):
    """A registered brand whose vetting call failed at filing. Telnyx
    lists ``listed`` vettings and answers a new request ``post_status``."""
    calls: list = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path.endswith("/externalVetting"):
            if isinstance(listed, int):
                return httpx.Response(listed)
            return httpx.Response(200, json=listed)
        if req.method == "POST" and req.url.path.endswith("/externalVetting"):
            return httpx.Response(post_status, json={"errors": post_errors or []})
        return httpx.Response(
            200, json={"status": "OK", "identityStatus": "SELF_DECLARED"}
        )

    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": {},
        "vetting_requested": False,
    }
    return _provider(handler), refs, calls


async def test_a_vetting_telnyx_already_has_is_not_asked_again() -> None:
    """The vetting call timed out at filing but reached Telnyx: each
    request is billed, so the background pass checks first."""
    p, refs, calls = _vetting_retry([{"evpId": "AEGIS", "vettingClass": "STANDARD"}])
    assert (await p.status(refs)).state == "pending"
    assert ("GET", "/v2/10dlc/brand/B1/externalVetting") in calls
    assert ("POST", "/v2/10dlc/brand/B1/externalVetting") not in calls
    assert refs["vetting_requested"] is True


async def test_a_pending_vetting_telnyx_already_has_is_not_asked_again() -> None:
    """Telnyx lists only ACTIVE vettings by default: the read names the
    running ones too, so a PENDING request is found and not billed twice."""
    seen: list = []

    def handler(req: httpx.Request):
        if req.method == "GET" and req.url.path.endswith("/externalVetting"):
            seen.append(sorted(req.url.params.get_list("vettingStatus")))
            return httpx.Response(200, json=[{"evpId": "AEGIS"}])
        if req.method == "POST":
            return httpx.Response(500)
        return httpx.Response(
            200, json={"status": "OK", "identityStatus": "SELF_DECLARED"}
        )

    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": {},
        "vetting_requested": False,
    }
    assert (await _provider(handler).status(refs)).state == "pending"
    assert seen == [["ACTIVE", "PENDING", "UNSCORE"]]
    assert refs["vetting_requested"] is True


@pytest.mark.parametrize("listed", [[], 404])
async def test_no_vetting_listed_asks_for_one(listed) -> None:
    p, refs, calls = _vetting_retry(listed)
    assert (await p.status(refs)).state == "pending"
    assert calls.count(("POST", "/v2/10dlc/brand/B1/externalVetting")) == 1
    assert refs["vetting_requested"] is True


async def test_a_duplicate_vetting_answer_counts_as_asked() -> None:
    """Telnyx answers 10012 when a vetting is in progress or passed."""
    p, refs, _ = _vetting_retry([], 400, [{"code": "10012"}])
    assert (await p.status(refs)).state == "pending"
    assert refs["vetting_requested"] is True


async def test_a_failed_vetting_list_asks_on_the_next_pass() -> None:
    p, refs, calls = _vetting_retry(503)
    with pytest.raises(VerificationProviderError):
        await p.status(refs)
    assert ("POST", "/v2/10dlc/brand/B1/externalVetting") not in calls
    assert refs["vetting_requested"] is False


async def test_a_brand_read_that_is_not_json_is_read_again() -> None:
    reads = iter([httpx.Response(200, content=b"<html>"), None])

    def handler(req: httpx.Request):
        if req.method == "POST" and req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            return next(reads) or httpx.Response(200, json={"status": "OK"})
        return httpx.Response(200, json={})

    draft = await _draft(_provider(handler))
    assert draft.problems == [] and draft.refs["vetting_requested"] is True


async def test_an_outage_on_the_brand_call_keeps_nothing() -> None:
    def handler(req):
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(502)
        return httpx.Response(204)

    with pytest.raises(VerificationProviderError):
        await _draft(_provider(handler))


async def test_a_4xx_on_the_brand_call_bills_nothing() -> None:
    def handler(req):
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(400, json={"errors": [{"code": "10015"}]})
        return httpx.Response(204)

    with pytest.raises(VerificationProviderError):
        await _draft(_provider(handler), subject="person", fields=PERSON_FIELDS)


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_campaign_replies_fall_back_to_the_saved_name(blank) -> None:
    from hailhq.core.providers.verification.telnyx_sms import _campaign_replies

    brand = {"displayName": blank, "firstName": blank, "lastName": blank}
    replies = _campaign_replies(brand, {"subject_name": " Acme  Inc "})
    assert replies["optoutMessage"].startswith("Acme Inc: You are unsubscribed")
    assert replies["helpMessage"].startswith("Acme Inc: Reply STOP")
    assert replies["optinMessage"].startswith("Acme Inc: You are opted in")
    # No name anywhere: no empty "": " prefix.
    nameless = _campaign_replies(brand, {"subject_name": blank})
    assert nameless["optoutMessage"].startswith("You are unsubscribed")


def test_help_reply_of_an_old_snapshot_names_hail_support() -> None:
    from hailhq.core.providers.verification.telnyx_sms import _campaign_replies

    replies = _campaign_replies({"displayName": "Acme"}, {"campaign": {}})
    assert replies["helpMessage"] == (
        "Acme: Reply STOP to opt out. For help contact hi@hail.so."
    )


def test_help_reply_fits_the_carrier_limit() -> None:
    from hailhq.core.providers.verification.telnyx_sms import _campaign_replies

    email = "a" * 60 + "@" + "b" * 60 + ".example"
    replies = _campaign_replies(
        {"displayName": "N" * 200}, {"campaign": {"contact_email": email}}
    )
    help_text = replies["helpMessage"]
    # The full name and that email do not fit: Hail's contact, full name.
    assert help_text == (
        "N" * 200 + ": Reply STOP to opt out. For help contact hi@hail.so."
    )
    # A long email that fits next to the name is kept.
    help_text = _campaign_replies(
        {"displayName": "Acme"}, {"campaign": {"contact_email": email}}
    )["helpMessage"]
    assert help_text.endswith(f"contact {email}.") and len(help_text) <= 255
    # An email that leaves no room for the name gives Hail's contact instead.
    huge = "a" * 240 + "@acme.example"
    help_text = _campaign_replies(
        {"displayName": "Acme"}, {"campaign": {"contact_email": huge}}
    )["helpMessage"]
    assert help_text == "Acme: Reply STOP to opt out. For help contact hi@hail.so."


def _replies_handler(calls, configs=(), fail=False):
    """Telnyx for a verified brand: files the campaign, serves and records
    the messaging profile's auto-replies."""

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"identityStatus": "VERIFIED", "displayName": "Acme"}
            )
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            return httpx.Response(200, json={"campaignId": "C1"})
        if req.url.path.endswith("/10dlc/campaign/C1"):
            return httpx.Response(200, json={"campaignStatus": "TCR_ACCEPTED"})
        if "/autoresp_configs" in req.url.path:
            if fail:
                return httpx.Response(500)
            if req.method == "GET":
                return httpx.Response(200, json={"data": list(configs)})
            return httpx.Response(200, json={"data": {"id": "new"}})
        return httpx.Response(404)

    return handler


def _filing_refs():
    return {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "subject_name": "Acme Inc",
        "campaign": {
            "use_case": "MIXED",
            "description": "d" * 40,
            "message_flow": "f" * 40,
            "samples": ["s" * 20, "t" * 20],
            "has_links": False,
            "schema_version": 2,
            "opt_in_message": "Acme: Actual enrollment confirmation. Reply HELP for help, STOP to stop.",
            "has_phone": False,
            "age_gated": False,
            "direct_lending": False,
            "text_types": ["CUSTOMER_CARE", "ACCOUNT_NOTIFICATION"],
            "contact_email": "help@acme.example",
        },
    }


async def test_status_sets_the_profile_auto_replies_to_the_filed_texts() -> None:
    calls = []
    p = _provider(_replies_handler(calls))
    refs = _filing_refs()
    assert (await p.status(refs)).state == "pending"
    filed = next(b for m, path, b in calls if path.endswith("/campaignBuilder"))
    sent = {
        b["op"]: b
        for m, path, b in calls
        if m == "POST" and path == "/v2/messaging_profiles/MP1/autoresp_configs"
    }
    assert sent["stop"]["resp_text"] == filed["optoutMessage"]
    assert sent["info"]["resp_text"] == filed["helpMessage"]
    assert filed["helpMessage"] == (
        "Acme: Reply STOP to opt out. For help contact help@acme.example."
    )
    assert sent["stop"]["keywords"] == [
        "STOP",
        "STOPALL",
        "UNSUBSCRIBE",
        "CANCEL",
        "END",
        "QUIT",
    ]
    assert sent["info"]["keywords"] == ["HELP", "INFO"]
    assert {b["country_code"] for b in sent.values()} == {"US"}
    assert refs["replies_set"] is True and refs["replies"]["optoutMessage"] == (
        filed["optoutMessage"]
    )
    # Set once: the next poll leaves the profile alone.
    calls.clear()
    assert (await p.status(refs)).state == "pending"
    assert not any("/autoresp_configs" in path for _, path, _ in calls)


async def test_status_updates_auto_replies_the_profile_already_has() -> None:
    calls = []
    # The list is asked for US only; items need not repeat country_code.
    configs = [
        {"id": "S1", "op": "stop"},
        {"id": "S2", "op": "stop", "country_code": "US"},
        {"id": "I1", "op": "info", "country_code": "CA"},
    ]
    p = _provider(_replies_handler(calls, configs))
    refs = _filing_refs()
    await p.status(refs)
    writes = [(m, path) for m, path, _ in calls if "/autoresp_configs" in path]
    get = next(c for c in calls if c[0] == "GET" and "/autoresp_configs" in c[1])
    assert get[1] == "/v2/messaging_profiles/MP1/autoresp_configs"
    # Matched on op, never a second config for an op that exists.
    assert writes == [
        ("GET", "/v2/messaging_profiles/MP1/autoresp_configs"),
        ("PUT", "/v2/messaging_profiles/MP1/autoresp_configs/S1"),
        ("PUT", "/v2/messaging_profiles/MP1/autoresp_configs/I1"),
    ]


async def test_status_keeps_working_when_the_auto_replies_fail() -> None:
    calls = []
    p = _provider(_replies_handler(calls, fail=True))
    refs = _filing_refs()
    assert (await p.status(refs)).state == "pending"
    assert refs["campaign_id"] == "C1" and "replies_set" not in refs
    # The next poll tries again, with the texts that were filed.
    calls.clear()
    assert (await p.status(refs)).state == "pending"
    assert any("/autoresp_configs" in path for _, path, _ in calls)


async def test_draft_without_a_profile_makes_the_organizations_profile() -> None:
    calls = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/messaging_profiles") and req.method == "GET":
            return httpx.Response(200, json={"data": []})
        if req.url.path.endswith("/messaging_profiles"):
            return httpx.Response(200, json={"data": {"id": "MP9"}})
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json={})
        return httpx.Response(404)

    p = _provider(handler)
    req = await p.requirements("US", "local", "business")
    draft = await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=FIELDS,
        address=ADDRESS,
        documents={},
        messaging_service_sid=None,
    )
    assert draft.problems == [] and draft.refs["messaging_profile_id"] == "MP9"
    assert calls[:2] == [
        ("GET", "/v2/messaging_profiles"),
        ("POST", "/v2/messaging_profiles"),
    ]


def _approved_handler(calls, *, replies_fail=False, record=None, profiles=None):
    """Telnyx for a campaign the carriers approved. ``record`` adds fields
    to the campaign read; ``profiles`` serves GET /messaging_profiles."""

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        path = req.url.path
        if path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"identityStatus": "VERIFIED", "displayName": "Acme"}
            )
        if path.endswith("/10dlc/campaign/C1"):
            return httpx.Response(
                200, json={"campaignStatus": "MNO_ACCEPTED", **(record or {})}
            )
        if "/autoresp_configs" in path:
            if replies_fail:
                return httpx.Response(500)
            if req.method == "GET":
                return httpx.Response(200, json={"data": []})
            return httpx.Response(200, json={"data": {"id": "new"}})
        if path.endswith("/messaging_profiles"):
            if req.method == "GET":
                return httpx.Response(200, json={"data": list(profiles or [])})
            return httpx.Response(200, json={"data": {"id": "MP7"}})
        return httpx.Response(404)

    return handler


_FILED = {
    "helpMessage": "Acme: Reply STOP to opt out. For help contact help@acme.example.",
    "optinMessage": (
        "Acme: You are opted in. Msg frequency varies. "
        "Msg and data rates may apply. Reply HELP for help, STOP to opt out."
    ),
    "optoutMessage": (
        "Acme: You are unsubscribed and will get no more messages from this workspace."
    ),
}


def _campaign_refs(**extra):
    return {
        "brand_id": "B1",
        "campaign_id": "C1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "campaign": {"contact_email": "help@acme.example"},
        "replies": dict(_FILED),
        **extra,
    }


def _reply_texts(calls):
    return {
        b["op"]: b["resp_text"]
        for m, path, b in calls
        if m in ("POST", "PUT") and "/autoresp_configs" in path
    }


async def test_approval_waits_until_the_auto_replies_are_set() -> None:
    """An approved row is not polled again, so approval waits for the
    replies: a failure on that poll is tried again on the next."""
    calls = []
    refs = _campaign_refs()
    failing = _provider(_approved_handler(calls, replies_fail=True))
    assert (await failing.status(refs)).state == "pending"
    assert "replies_set" not in refs
    assert refs["campaign_accepted"] is True and refs["runtime_ready"] is False
    assert failing.step(refs) == "runtime_setup"
    working = _provider(_approved_handler(calls))
    assert (await working.status(refs)).state == "approved"
    assert refs["replies_set"] is True
    assert _reply_texts(calls) == {
        "stop": _FILED["optoutMessage"],
        "info": _FILED["helpMessage"],
    }


async def test_failed_runtime_setup_never_approves_or_refiles() -> None:
    calls = []
    p = _provider(_approved_handler(calls, replies_fail=True))
    refs = _campaign_refs()
    for _ in range(12):
        assert (await p.status(refs)).state == "pending"
    assert refs["campaign_accepted"] is True
    assert refs["runtime_ready"] is False
    assert p.step(refs) == "runtime_setup"
    assert not any(path.endswith("campaignBuilder") for _, path, _ in calls)


async def test_approved_at_once_when_the_replies_are_set() -> None:
    calls = []
    refs = _campaign_refs(replies_set=True)
    assert (await _provider(_approved_handler(calls)).status(refs)).state == (
        "approved"
    )
    assert not any("/autoresp_configs" in path for _, path, _ in calls)
    assert "replies_waits" not in refs


@pytest.mark.parametrize(
    "replies",
    [
        {"helpMessage": "Acme: help"},  # a key missing
        {"helpMessage": "h", "optoutMessage": None},
        "not a dict",
        ["a", "b"],
    ],
)
async def test_odd_saved_replies_are_logged_not_raised(replies, caplog) -> None:
    calls = []
    p = _provider(_approved_handler(calls))
    refs = _campaign_refs(replies=replies)
    with caplog.at_level("WARNING"):
        await p._set_replies(refs)
    assert "replies_set" not in refs
    assert not any("/autoresp_configs" in path for _, path, _ in calls)
    assert "10DLC auto-replies not set" in caplog.text


async def test_odd_saved_replies_are_replaced_by_the_campaign_texts() -> None:
    """A poll never fails on odd saved texts: it takes Telnyx's."""
    calls = []
    p = _provider(_approved_handler(calls, record=_FILED))
    refs = _campaign_refs(replies={"helpMessage": "x"})
    assert (await p.status(refs)).state == "approved"
    assert refs["replies"] == _FILED and refs["replies_set"] is True


@pytest.mark.parametrize(
    "profiles",
    [
        # Two profiles with the org's name: ensure_messaging_service raises
        # ValueError.
        [{"id": "P1", "name": "hail-org1"}, {"id": "P2", "name": "hail-org1"}],
        None,  # Telnyx answers 503
    ],
)
async def test_draft_files_even_when_the_profile_fails(profiles, caplog) -> None:
    """A profile error is logged, and the brand is filed without one."""
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/messaging_profiles"):
            if profiles is None:
                return httpx.Response(503)
            return httpx.Response(200, json={"data": profiles})
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"status": "OK"})
        if req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json=[] if req.method == "GET" else {})
        return httpx.Response(404)

    p = _provider(handler)
    req = await p.requirements("US", "local", "business")
    with caplog.at_level("WARNING"):
        draft = await p.create_draft(
            organization_id="org1",
            contact_email="ops@hail.so",
            requirements=req,
            fields=FIELDS,
            address=ADDRESS,
            documents={},
            messaging_service_sid=None,
        )
    assert draft.problems == [] and draft.refs["brand_id"] == "B1"
    assert draft.refs["messaging_profile_id"] is None
    assert draft.refs["organization_id"] == "org1"
    assert ("POST", "/v2/10dlc/brand") in calls
    assert ("POST", "/v2/10dlc/brand/B1/externalVetting") in calls
    assert draft.refs.get("vetting_requested") is not False
    assert "10DLC messaging profile not ready at filing" in caplog.text


async def test_replies_find_the_org_profile_when_the_filing_has_none() -> None:
    calls = []
    p = _provider(_approved_handler(calls))
    refs = _campaign_refs(messaging_profile_id=None, organization_id="org1")
    assert (await p.status(refs)).state == "approved"
    assert refs["messaging_profile_id"] == "MP7" and refs["replies_set"] is True
    profile_calls = [(m, path) for m, path, _ in calls if "messaging_profiles" in path]
    assert profile_calls[:2] == [
        ("GET", "/v2/messaging_profiles"),
        ("POST", "/v2/messaging_profiles"),
    ]
    assert ("POST", "/v2/messaging_profiles/MP7/autoresp_configs") in profile_calls


async def test_replies_skip_an_old_filing_with_no_profile_or_org(caplog) -> None:
    calls = []
    p = _provider(_approved_handler(calls))
    refs = _campaign_refs(messaging_profile_id=None)
    with caplog.at_level("WARNING"):
        assert (await p.status(refs)).state == "pending"
    assert not any("messaging_profiles" in path for _, path, _ in calls)
    assert "no messaging profile or organization saved" in caplog.text


async def test_adopted_campaign_keeps_the_texts_telnyx_has() -> None:
    """A campaign filed by a lost call: its texts come from Telnyx's
    record, and the auto-replies are those texts."""
    calls = []
    record = {
        "campaignId": "C9",
        "helpMessage": "Acme Co: For help write to us.",
        "optinMessage": "Acme Co: opted in.",
        "optoutMessage": "Acme Co: unsubscribed.",
    }

    def handler(req):
        body = json.loads(req.content) if req.content else None
        calls.append((req.method, req.url.path, body))
        if req.method == "GET" and req.url.path.endswith("/10dlc/campaign"):
            return httpx.Response(200, json={"records": [record]})
        if "/autoresp_configs" in req.url.path:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    refs = {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "campaign": {},
    }
    assert (await _provider(handler).status(refs)).state == "pending"
    assert refs["campaign_id"] == "C9"
    assert refs["replies"] == {
        "helpMessage": "Acme Co: For help write to us.",
        "optinMessage": "Acme Co: opted in.",
        "optoutMessage": "Acme Co: unsubscribed.",
    }
    assert _reply_texts(calls) == {
        "stop": "Acme Co: unsubscribed.",
        "info": "Acme Co: For help write to us.",
    }
    assert not any(path.endswith("/campaignBuilder") for _, path, _ in calls)


async def test_adopted_campaign_without_texts_builds_them() -> None:
    def handler(req):
        if req.method == "GET" and req.url.path.endswith("/10dlc/campaign"):
            return httpx.Response(200, json={"records": [{"campaignId": "C9"}]})
        if "/autoresp_configs" in req.url.path:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200, json={"identityStatus": "VERIFIED", "displayName": "Acme"}
        )

    refs = {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "campaign": {"contact_email": "help@acme.example"},
    }
    await _provider(handler).status(refs)
    assert refs["replies"] == _FILED


async def test_an_old_row_takes_its_texts_once_from_the_campaign() -> None:
    """A row filed before the texts were kept takes Telnyx's, keeps them,
    and later polls use the kept ones."""
    calls = []
    record = {"helpMessage": "Old: help.", "optoutMessage": "Old: bye."}
    p = _provider(_approved_handler(calls, replies_fail=True, record=record))
    refs = _campaign_refs()
    del refs["replies"]
    await p.status(refs)
    kept = refs["replies"]
    assert kept["helpMessage"] == "Old: help." and kept["optoutMessage"] == "Old: bye."
    # optinMessage missing from the record: built as Hail files it.
    assert kept["optinMessage"] == _FILED["optinMessage"]
    calls.clear()
    p = _provider(
        _approved_handler(calls, record={"helpMessage": "Changed.", **record})
    )
    await p.status(refs)
    assert refs["replies"] is kept and kept["helpMessage"] == "Old: help."
    assert _reply_texts(calls)["info"] == "Old: help."


def test_every_reply_caps_the_name() -> None:
    from hailhq.core.providers.verification.telnyx_sms import _campaign_replies

    words = " ".join(["Acme"] * 80)  # 399 characters
    replies = _campaign_replies({"displayName": words}, {"campaign": {}})
    for key, text in replies.items():
        assert len(text) <= 255, key
        name, _ = text.split(": ", 1)
        # Cut at a word boundary.
        assert name.split(" ")[-1] == "Acme", key
    assert replies["optoutMessage"].endswith(
        ": You are unsubscribed and will get no more messages from this workspace."
    )
    assert replies["optinMessage"].endswith(
        ": You are opted in. Msg frequency varies. "
        "Msg and data rates may apply. Reply HELP for help, STOP to opt out."
    )


def test_a_name_with_no_room_for_a_word_boundary_is_cut_mid_word() -> None:
    from hailhq.core.providers.verification.telnyx_sms import _named

    text = "t" * 200
    name = "Ab " + "c" * 100  # the only space leaves 2 characters
    out = _named(name, text)
    assert len(out) == 255 and out.startswith("Ab " + "c" * 50)


def test_a_long_contact_email_gives_hail_support_not_a_cut_name() -> None:
    from hailhq.core.providers.verification.telnyx_sms import _campaign_replies

    name = "Acme Dental Care of Austin"
    email = "a" * 190 + "@acme.example"  # fits alone, not with the name
    help_text = _campaign_replies(
        {"displayName": name}, {"campaign": {"contact_email": email}}
    )["helpMessage"]
    assert help_text == (f"{name}: Reply STOP to opt out. For help contact hi@hail.so.")


async def test_single_purpose_payload_uses_all_real_attributes_and_samples():
    posted = []

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    text = {
        "schema_version": 2,
        "use_case": "CUSTOMER_CARE",
        "text_types": ["CUSTOMER_CARE"],
        "description": "Actual support replies",
        "message_flow": "Actual consent flow",
        "samples": [f"Acme sample {i}" for i in range(5)],
        "has_links": True,
        "has_phone": True,
        "age_gated": True,
        "direct_lending": True,
        "privacy_policy_url": "https://acme.org/privacy",
        "terms_url": "https://acme.org/terms",
        "opt_in_message": "Acme: your actual subscription confirmation.",
        "opt_in_keywords": ["JOIN"],
    }
    await _provider(handler).status(
        {"brand_id": "B1", "subject_type": "business", "campaign": text}
    )
    assert len(posted) == 1
    body = posted[0]
    assert body["usecase"] == "CUSTOMER_CARE" and "subUsecases" not in body
    assert [body[f"sample{i}"] for i in range(1, 6)] == text["samples"]
    assert body["embeddedPhone"] and body["ageGated"] and body["directLending"]
    assert body["privacyPolicyLink"] == text["privacy_policy_url"]
    assert body["termsAndConditionsLink"] == text["terms_url"]
    assert body["optinMessage"] == text["opt_in_message"]
    assert body["optinKeywords"] == "JOIN"


def test_new_application_does_not_invent_subscription_confirmation():
    replies = telnyx_sms._campaign_replies(
        {"displayName": "Acme"}, {"campaign": {"schema_version": 2}}
    )
    assert "optinMessage" not in replies
    assert "workspace" in replies["optoutMessage"]


@pytest.mark.parametrize("mobile", ["+12425550123", "+17875550123", "+447700900123"])
async def test_person_phone_geo_is_checked_before_carrier_calls(mobile):
    calls = []
    provider = _provider(lambda req: calls.append(req) or httpx.Response(500))
    result = await _draft(
        provider, "person", {**PERSON_FIELDS, "mobile_phone_number": mobile}
    )
    assert any(p.field == "mobile_phone_number" for p in result.problems)
    assert not calls


async def test_address_country_must_match_eligibility():
    provider = _provider(lambda req: pytest.fail("must not contact carrier"))
    result = await _draft(provider, fields={**FIELDS, "sender_country": "CA"})
    assert any(p.field == "address.country_code" for p in result.problems)


def test_repair_reason_gives_guidance_then_the_carriers_words():

    message = campaign_repair_reason("Privacy policy link returns 404")
    assert message.startswith("Review your privacy policy")
    assert message.endswith("The carrier said: Privacy policy link returns 404.")
    # Structured errors quote their text, never the dict.
    message = campaign_repair_reason([{"code": "1", "detail": "Sample 2 is vague"}])
    assert message.endswith("The carrier said: Sample 2 is vague.")
    assert "{" not in message
    # Key names and links in the structure never pick the topic.
    message = campaign_repair_reason(
        [{"code": "1", "detail": "Campaign rejected", "meta": {"url": "https://x"}}]
    )
    assert message.startswith(CAMPAIGN_REJECTED)


async def test_campaign_submission_refusal_returns_safe_repair_guidance():
    from hailhq.core.providers.verification.sms_form import campaign_text

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/campaignBuilder"):
            return httpx.Response(
                400,
                json={
                    "errors": [
                        {
                            "detail": "Privacy policy absent for Ada Secret EIN 12-3456789"
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": campaign_text(FIELDS, "business").__dict__,
    }
    result = await _provider(handler).status(refs)
    assert result.state == "pending" and "privacy policy" in result.reason
    assert result.reason.endswith(
        "The carrier said: Privacy policy absent for Ada Secret EIN 12-3456789."
    )
    assert "Ada Secret" in result.raw_reason
    assert "campaign_id" not in refs
    # Held for the customer's answers on the same paid brand.
    assert refs["needs_information"] is True and refs["brand_id"] == "B1"


@pytest.mark.parametrize("status", [400, 422])
@pytest.mark.parametrize(
    "field",
    [
        "messageFlow",
        "usecase",
        "subUsecases",
        "description",
        "sample1",
        "sample2",
        "sample3",
        "sample4",
        "sample5",
        "embeddedLink",
        "embeddedPhone",
        "ageGated",
        "directLending",
        "subscriberOptin",
        "subscriberOptout",
        "subscriberHelp",
        "optinKeywords",
        "optoutKeywords",
        "helpKeywords",
        "optinMessage",
        "optoutMessage",
        "helpMessage",
        "termsAndConditions",
        "privacyPolicyLink",
        "termsAndConditionsLink",
    ],
)
async def test_campaign_structured_validation_requests_repair(status, field):
    from hailhq.core.providers.verification.sms_form import campaign_text

    writes = []

    def handler(req):
        if req.method != "GET":
            writes.append((req.method, req.url.path))
            return httpx.Response(
                status,
                json={
                    "errors": [
                        {"detail": "Invalid value", "source": {"pointer": f"/{field}"}}
                    ]
                },
            )
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": campaign_text(FIELDS, "business").__dict__,
    }
    result = await _provider(handler).status(refs)
    assert result.state == "pending" and result.reason
    assert refs["campaign_needs_repair"] is True
    assert refs["needs_information"] is True
    assert refs["brand_id"] == "B1" and "campaign_id" not in refs
    assert writes == [("POST", "/v2/10dlc/campaignBuilder")]
    if field == "messageFlow":
        assert "consent" in result.reason
    if field == "usecase":
        assert "purposes" in result.reason


@pytest.mark.parametrize(
    ("status", "pointer", "detail"),
    [
        (400, "/brandId", "Invalid value"),
        (422, "/numberPool", "Invalid value"),
        (422, "/messagingProfileId", "Invalid value"),
        (400, "/unknown", "Invalid value"),
        (422, "/account/email", "Invalid value"),
        (401, "/messageFlow", "Invalid value"),
        (403, "/usecase", "Invalid value"),
        (400, "/messageFlow", "Account configuration missing"),
        (422, "/usecase", "Invalid API key"),
    ],
)
async def test_campaign_operational_pointers_remain_retryable(status, pointer, detail):
    from hailhq.core.providers.verification.sms_form import campaign_text

    writes = []

    def handler(req):
        if req.method != "GET":
            writes.append((req.method, req.url.path))
            return httpx.Response(
                status,
                json={"errors": [{"detail": detail, "source": {"pointer": pointer}}]},
            )
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": campaign_text(FIELDS, "business").__dict__,
    }
    with pytest.raises(VerificationProviderError):
        await _provider(handler).status(refs)
    assert "campaign_needs_repair" not in refs and "campaign_id" not in refs
    assert refs["brand_id"] == "B1"
    assert writes == [("POST", "/v2/10dlc/campaignBuilder")]


async def test_legacy_confirmed_person_keeps_paid_brand_for_evidence_repair():
    calls = []

    def handler(req):
        calls.append((req.method, req.url.path))
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    provider = _provider(handler)
    refs = {"subject_type": "person", "brand_id": "B1", "campaign": {}}
    assert (await provider.status(refs)).state == "pending"
    assert provider.step(refs) == "needs_information"
    assert refs["brand_id"] == "B1"
    assert not any(method in {"DELETE", "POST"} for method, _ in calls)


async def test_telnyx_requires_actual_confirmation_before_paid_brand():
    calls = []
    provider = _provider(lambda req: calls.append(req) or httpx.Response(500))
    fields = {**FIELDS, "opt_in_message": ""}
    result = await _draft(provider, fields=fields)
    assert any(problem.field == "opt_in_message" for problem in result.problems)
    assert calls == []
    req = await provider.requirements("US", "local", "business")
    assert next(spec for spec in req.fields if spec.name == "opt_in_message").required


@pytest.mark.parametrize(
    "status,detail",
    [
        (401, "Invalid credentials"),
        (403, "Forbidden"),
        (404, "Not found"),
        (409, "Conflict"),
        (429, "Slow down"),
        (400, "Account configuration missing"),
        (422, "Messaging service configuration invalid"),
    ],
)
async def test_campaign_operational_errors_keep_the_brand_retryable(status, detail):
    from hailhq.core.providers.verification.sms_form import campaign_text

    def handler(req):
        if req.method == "POST" and req.url.path.endswith("/campaignBuilder"):
            return httpx.Response(status, json={"errors": [{"detail": detail}]})
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    refs = {
        "brand_id": "B1",
        "subject_type": "business",
        "campaign": campaign_text(FIELDS, "business").__dict__,
    }
    with pytest.raises(VerificationProviderError):
        await _provider(handler).status(refs)
    assert refs["brand_id"] == "B1" and "campaign_needs_repair" not in refs


@pytest.mark.parametrize("subject", ["person", "business"])
# No text at all: deleted when the account closed.
@pytest.mark.parametrize("saved", [{"campaign": {}}, {}])
async def test_legacy_sender_waits_for_information_without_new_paid_resources(
    subject, saved
):
    calls = []

    def handler(req):
        calls.append(req.method)
        return httpx.Response(200, json={"identityStatus": "VERIFIED"})

    provider = _provider(handler)
    refs = {"brand_id": "B1", "subject_type": subject, **saved}
    assert (await provider.status(refs)).state == "pending"
    assert provider.step(refs) == "needs_information" and refs["brand_id"] == "B1"
    assert set(calls) == {"GET"}


async def test_telnyx_recovers_authoritative_person_identity_readonly():
    calls = []
    brand = {
        **KEPT_BRAND,
        "country": "US",
        "street": ADDRESS.street,
        "city": ADDRESS.city,
        "state": ADDRESS.region,
        "postalCode": ADDRESS.postal_code,
    }
    provider = _provider(
        lambda req: calls.append(req.method) or httpx.Response(200, json=brand)
    )
    identity = await provider.application_identity({"brand_id": "B9"}, "person")
    assert identity["fields"]["email"].casefold() == PERSON_FIELDS["email"].casefold()
    assert identity["address"]["street"] == ADDRESS.street and calls == ["GET"]


async def test_invalid_confirmation_content_never_creates_a_paid_brand():
    calls = []
    provider = _provider(lambda req: calls.append(req) or httpx.Response(500))
    draft = await _draft(
        provider,
        fields={
            **FIELDS,
            "opt_in_message": "Thank you, your subscription is now active.",
        },
    )
    assert any(problem.field == "opt_in_message" for problem in draft.problems)
    assert calls == []


def test_body_pointer_names_an_application_field() -> None:
    """Telnyx points at campaign fields as "/body/<field>" too."""
    from hailhq.core.providers.verification.telnyx_sms import _refused

    request = httpx.Request("POST", "https://api.telnyx.com/v2/10dlc/campaignBuilder")
    response = httpx.Response(
        400,
        request=request,
        json={
            "errors": [
                {
                    "code": "10015",
                    "detail": "optoutMessage is required if subscriberOptout is true.",
                    "source": {"pointer": "/body/optoutMessage"},
                }
            ]
        },
    )
    exc = httpx.HTTPStatusError("400", request=request, response=response)
    assert _refused(exc)


def test_repair_reason_matches_whole_word_starts_only() -> None:

    assert campaign_repair_reason("for example, it determines nothing") == (
        f"{CAMPAIGN_REJECTED} The carrier said: for example, it determines nothing."
    )
    assert "message examples" in campaign_repair_reason("Samples are generic")
    for raw in ("Invalid MessageSamples", "MESSAGE_SAMPLES", "/body/sample1"):
        assert "message examples" in campaign_repair_reason(raw)
    assert "selected purposes" in campaign_repair_reason("subUsecases is invalid")
    assert "application links" in campaign_repair_reason("HasEmbeddedLinks")


def test_same_sender_ignores_website_scheme_and_trailing_slash() -> None:
    from hailhq.core.providers.verification.base import Address
    from hailhq.core.providers.verification.sms_review import same_sender

    location = {
        "street": "1 Main St",
        "city": "Austin",
        "region": "TX",
        "postal_code": "78701",
        "country_code": "US",
    }
    original = {
        "fields": {
            "legal_name": "Acme Inc",
            "ein": "123456789",
            "registration_country": "US",
            "website": "http://acme.com/",
        },
        "address": location,
    }
    fields = {
        "legal_name": "Acme Inc",
        "ein": "12-3456789",
        "registration_country": "US",
        "website": "https://acme.com",
    }
    address = Address(customer_name="Acme Inc", **location)
    assert same_sender(original, fields, address, "business")
    assert not same_sender(
        original, {**fields, "website": "https://other.com"}, address, "business"
    )


def test_same_sender_reads_a_vat_number_with_or_without_its_prefix() -> None:
    from hailhq.core.providers.verification.base import Address
    from hailhq.core.providers.verification.sms_review import same_sender

    location = {
        "street": "Drottninggatan 1",
        "city": "Stockholm",
        "region": "Stockholm",
        "postal_code": "111 51",
        "country_code": "SE",
    }
    original = {
        "fields": {
            "legal_name": "Acme AB",
            "ein": "556677889901",
            "registration_country": "SE",
        },
        "address": location,
    }
    fields = {
        "legal_name": "Acme AB",
        "ein": "SE 556677-8899 01",
        "registration_country": "SE",
    }
    address = Address(customer_name="Acme AB", **location)
    assert same_sender(original, fields, address, "business")
    assert not same_sender(
        original, {**fields, "ein": "SE556677889902"}, address, "business"
    )


LOW_VOLUME_FIELDS = {**FIELDS, "daily_texts": "UP_TO_2000"}


async def _low_volume_draft(p, **kwargs):
    return await _draft(p, fields=LOW_VOLUME_FIELDS, low_volume=True, **kwargs)


def _low_volume_brand(calls: list, brand_reads, vetting_status: int = 200):
    """Telnyx makes brand B1; each GET of it answers the next of
    ``brand_reads`` (a status code or a JSON body), the last one repeating.
    A vetting request answers ``vetting_status``."""
    reads = list(brand_reads)

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "POST" and req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            answer = reads.pop(0) if len(reads) > 1 else reads[0]
            if isinstance(answer, int):
                return httpx.Response(answer)
            return httpx.Response(200, json=answer)
        if req.url.path.endswith("/externalVetting"):
            if req.method == "GET":
                return httpx.Response(200, json=[])
            return httpx.Response(vetting_status, json={})
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    return _provider(handler)


def _vetting_calls(calls: list) -> list:
    return [c for c in calls if c == ("POST", "/v2/10dlc/brand/B1/externalVetting")]


def _filable_campaign(use_case: str, purposes: list[str]) -> dict:
    return {
        **_unfiled_campaign_refs()["campaign"],
        "use_case": use_case,
        "text_types": purposes,
        "has_phone": False,
        "age_gated": False,
        "direct_lending": False,
    }


def _low_volume_refs(**extra) -> dict:
    return {
        "brand_id": "B1",
        "messaging_profile_id": "MP1",
        "subject_type": "business",
        "low_volume": True,
        "campaign": _filable_campaign("LOW_VOLUME", ["CUSTOMER_CARE"]),
        **extra,
    }


async def test_low_volume_company_brand_is_never_vetted_at_filing() -> None:
    calls: list = []
    p = _low_volume_brand(calls, [{"status": "OK", "identityStatus": "VERIFIED"}])
    draft = await _low_volume_draft(p)
    assert draft.problems == [] and draft.refs["brand_id"] == "B1"
    assert _vetting_calls(calls) == []
    assert "vetting_requested" not in draft.refs
    assert draft.refs["campaign"]["use_case"] == "LOW_VOLUME"
    # The API writes the fee marker, from the decision it priced.
    assert "low_volume" not in draft.refs


async def test_low_volume_brand_still_registering_owes_no_vetting() -> None:
    calls: list = []
    draft = await _low_volume_draft(_low_volume_brand(calls, [404]))
    assert draft.problems == [] and draft.refs["brand_id"] == "B1"
    assert "vetting_requested" not in draft.refs
    assert _vetting_calls(calls) == []


async def test_a_failure_after_the_low_volume_brand_owes_no_vetting() -> None:
    calls: list = []
    # The brand read fails with an answer that is not a refusal.
    draft = await _low_volume_draft(_low_volume_brand(calls, [401]))
    assert draft.problems == [] and draft.refs["brand_id"] == "B1"
    assert "vetting_requested" not in draft.refs
    assert _vetting_calls(calls) == []


@pytest.mark.parametrize("owed", [False, True, None])
@pytest.mark.parametrize("faster_lane_ok", [False, True])
async def test_a_low_volume_brand_still_being_checked_waits_a_day(
    owed, faster_lane_ok
) -> None:
    calls: list = []
    p = _low_volume_brand(calls, [{"status": "OK", "identityStatus": "SELF_DECLARED"}])
    refs = _low_volume_refs()
    if owed is not None:
        refs["vetting_requested"] = owed
    if faster_lane_ok:
        refs["faster_lane_ok"] = True
    for _ in range(3):
        assert (await p.status(refs)).state == "pending"
    assert not any(m == "POST" for m, _ in calls)
    assert "needs_information" not in refs and "faster_lane_at" not in refs
    assert datetime.fromisoformat(refs["identity_wait_since"]).tzinfo is not None


@pytest.mark.parametrize("identity", ["SELF_DECLARED", None, "SOMETHING_NEW"])
@pytest.mark.parametrize("faster_lane_ok", [False, True])
async def test_a_brand_still_not_verified_after_a_day_takes_the_faster_lane_path(
    identity, faster_lane_ok
) -> None:
    """Not verified a day after Telnyx registered it: never pending forever."""
    calls: list = []
    brand = {"status": "OK", **({"identityStatus": identity} if identity else {})}
    p = _low_volume_brand(calls, [brand])
    day_ago = datetime.now(timezone.utc) - timedelta(hours=25)
    refs = _low_volume_refs(identity_wait_since=day_ago.isoformat())
    if faster_lane_ok:
        refs["faster_lane_ok"] = True
    await p.status(refs)
    if faster_lane_ok:
        assert len(_vetting_calls(calls)) == 1 and refs["faster_lane_at"]
    else:
        assert refs["needs_information"] is True and _vetting_calls(calls) == []


@pytest.mark.parametrize("identity", ["UNVERIFIED", "SELF_DECLARED"])
async def test_the_faster_lane_waits_for_credit(identity) -> None:
    """Not covered (``may_file_campaign`` False): nothing is bought; a
    later pass with credit buys it."""
    calls: list = []
    p = _low_volume_brand(calls, [{"status": "OK", "identityStatus": identity}])
    day_ago = datetime.now(timezone.utc) - timedelta(hours=25)
    refs = _low_volume_refs(
        faster_lane_ok=True, identity_wait_since=day_ago.isoformat()
    )
    st = await p.status(refs, may_file_campaign=False)
    assert st.state == "pending"
    assert not any(m == "POST" for m, _ in calls)
    assert "faster_lane_at" not in refs and "needs_information" not in refs
    await p.status(refs)
    assert len(_vetting_calls(calls)) == 1 and refs["faster_lane_at"]


async def test_an_unverified_low_volume_brand_offers_the_faster_lane() -> None:
    from hailhq.core.providers.verification.sms_review import LOW_VOLUME_FASTER_LANE

    calls: list = []
    p = _low_volume_brand(calls, [{"status": "OK", "identityStatus": "UNVERIFIED"}])
    refs = _low_volume_refs()
    st = await p.status(refs)
    assert st.state == "pending" and st.reason == LOW_VOLUME_FASTER_LANE
    assert refs["needs_information"] is True
    assert not any(m == "POST" for m, _ in calls)
    assert "faster_lane_at" not in refs and "vetting_requested" not in refs


async def test_a_ticked_faster_lane_vets_an_unverified_brand_once() -> None:
    calls: list = []
    reads = [
        {"status": "OK", "identityStatus": "UNVERIFIED"},
        {"status": "OK", "identityStatus": "UNVERIFIED"},
        {"status": "OK", "identityStatus": "VETTED_VERIFIED"},
    ]
    p = _low_volume_brand(calls, reads)
    refs = _low_volume_refs(faster_lane_ok=True)
    assert (await p.status(refs)).state == "pending"
    assert _vetting_calls(calls) == [("POST", "/v2/10dlc/brand/B1/externalVetting")]
    # The fee rater charges the vetting from this mark.
    assert datetime.fromisoformat(refs["faster_lane_at"]).tzinfo is not None
    assert refs["vetting_requested"] is True and p.in_review(refs)
    assert "needs_information" not in refs
    # Still unverified while vetting runs: no second request.
    assert (await p.status(refs)).state == "pending"
    assert len(_vetting_calls(calls)) == 1
    # Vetted: the normal flow files the low-volume campaign.
    assert (await p.status(refs)).state == "pending"
    assert refs["campaign_id"] == "C1"
    assert len(_vetting_calls(calls)) == 1


async def test_a_faster_lane_vetting_telnyx_already_has_is_not_asked_again() -> None:
    calls: list = []

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "OK", "identityStatus": "UNVERIFIED"}
            )
        if req.method == "GET" and req.url.path.endswith("/externalVetting"):
            return httpx.Response(200, json=[{"evpId": "AEGIS"}])
        return httpx.Response(404)

    refs = _low_volume_refs(faster_lane_ok=True)
    assert (await _provider(handler).status(refs)).state == "pending"
    assert not any(m == "POST" for m, _ in calls)
    assert refs["vetting_requested"] is True and refs["faster_lane_at"]


async def test_a_failed_faster_lane_request_is_asked_again_on_the_next_pass() -> None:
    calls: list = []
    p = _low_volume_brand(
        calls, [{"status": "OK", "identityStatus": "UNVERIFIED"}], vetting_status=502
    )
    refs = _low_volume_refs(faster_lane_ok=True)
    with pytest.raises(VerificationProviderError):
        await p.status(refs)
    # Nothing bought, so nothing to charge: the lane stays owed.
    assert "faster_lane_at" not in refs and not refs.get("vetting_requested")
    ok = _low_volume_brand(calls, [{"status": "OK", "identityStatus": "UNVERIFIED"}])
    calls.clear()
    assert (await ok.status(refs)).state == "pending"
    assert _vetting_calls(calls) == [("POST", "/v2/10dlc/brand/B1/externalVetting")]
    assert refs["vetting_requested"] is True and refs["faster_lane_at"]


async def test_no_faster_lane_hold_once_a_campaign_is_filed() -> None:
    calls: list = []
    p = _low_volume_brand(calls, [{"status": "OK", "identityStatus": "UNVERIFIED"}])
    refs = _low_volume_refs()
    refs["campaign_id"] = "C1"
    assert (await p.status(refs)).state == "pending"
    assert "needs_information" not in refs and not _vetting_calls(calls)


async def test_low_volume_campaign_files_its_purposes_as_sub_use_cases() -> None:
    posted = []

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "OK", "identityStatus": "VERIFIED"}
            )
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    for purposes in (["CUSTOMER_CARE"], ["CUSTOMER_CARE", "MARKETING"]):
        posted.clear()
        refs = _low_volume_refs(campaign=_filable_campaign("LOW_VOLUME", purposes))
        assert (await _provider(handler).status(refs)).state == "pending"
        assert posted[0]["usecase"] == "LOW_VOLUME"
        assert posted[0]["subUsecases"] == purposes


async def test_a_single_purpose_vetted_company_files_no_sub_use_cases() -> None:
    posted = []

    def handler(req):
        if req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(200, json={"identityStatus": "VETTED_VERIFIED"})
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            posted.append(json.loads(req.content))
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    refs = _unfiled_campaign_refs()
    refs["campaign"] = _filable_campaign("CUSTOMER_CARE", ["CUSTOMER_CARE"])
    await _provider(handler).status(refs)
    assert posted[0]["usecase"] == "CUSTOMER_CARE"
    assert "subUsecases" not in posted[0]


async def test_a_person_never_gets_the_low_volume_brand() -> None:
    def handler(req: httpx.Request):
        if req.url.path.endswith("/10dlc/brand"):
            return httpx.Response(200, json={"brandId": "B1"})
        if req.method == "GET":
            return httpx.Response(200, json={"status": "OK"})
        return httpx.Response(200, json={"referenceId": "R1"})

    draft = await _draft(
        _provider(handler),
        subject="person",
        fields={**PERSON_FIELDS, "daily_texts": "UP_TO_2000"},
        low_volume=True,
    )
    assert draft.problems == []
    assert draft.refs["campaign"]["use_case"] == "SOLE_PROPRIETOR"


async def test_a_verified_low_volume_brand_marks_the_lane_not_owed() -> None:
    """Verified, even on a pass that may not file the campaign: the mark
    stops the credit checks counting the faster lane, and no later read
    buys it."""
    calls: list = []
    p = _low_volume_brand(
        calls,
        [
            {"status": "OK", "identityStatus": "VERIFIED"},
            {"status": "OK", "identityStatus": "UNVERIFIED"},
        ],
    )
    refs = _low_volume_refs(faster_lane_ok=True)
    assert (await p.status(refs, may_file_campaign=False)).state == "pending"
    assert refs["brand_verified"] is True
    assert "campaign_id" not in refs
    # A later read that lags behind buys no lane and holds nothing.
    assert (await p.status(refs)).state == "pending"
    assert _vetting_calls(calls) == []
    assert "faster_lane_at" not in refs and "needs_information" not in refs


async def test_a_vetted_company_gets_no_lane_mark() -> None:
    calls: list = []
    p = _low_volume_brand(calls, [{"status": "OK", "identityStatus": "VERIFIED"}])
    refs = _low_volume_refs()
    refs.pop("low_volume")
    await p.status(refs, may_file_campaign=False)
    assert "brand_verified" not in refs


def _bought_lane(calls: list, vettings, identity: str = "UNVERIFIED"):
    """A low-volume brand whose faster lane was bought; the vetting list
    answers ``vettings`` (a status code or a JSON body)."""

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path, req.url.params))
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "OK", "identityStatus": identity}
            )
        if req.method == "GET" and req.url.path.endswith("/externalVetting"):
            if isinstance(vettings, int):
                return httpx.Response(vettings)
            return httpx.Response(200, json=vettings)
        return httpx.Response(404)

    refs = _low_volume_refs(
        faster_lane_ok=True,
        faster_lane_at=datetime.now(timezone.utc).isoformat(),
        vetting_requested=True,
    )
    return _provider(handler), refs


@pytest.mark.parametrize("status", ["FAILED", "EXPIRED"])
async def test_a_failed_faster_lane_holds_the_row(status) -> None:
    from hailhq.core.providers.verification.sms_review import (
        LOW_VOLUME_FASTER_LANE_FAILED,
    )

    calls: list = []
    p, refs = _bought_lane(calls, [{"evpId": "AEGIS", "vettingStatus": status}])
    st = await p.status(refs)
    assert st.state == "pending" and st.reason == LOW_VOLUME_FASTER_LANE_FAILED
    assert refs["needs_information"] is True
    # The customer can act: the row reads as held, not as in review.
    assert p.step(refs) == "needs_information"
    # Telnyx lists only ACTIVE vettings unless asked for the others.
    params = [c[2] for c in calls if c[1].endswith("/externalVetting")]
    assert sorted(params[0].get_list("vettingStatus")) == [
        "ACTIVE",
        "EXPIRED",
        "FAILED",
    ]
    assert not any(c[0] == "POST" for c in calls)


@pytest.mark.parametrize(
    "vettings",
    [
        [],
        404,
        [{"evpId": "AEGIS"}],
        [{"evpId": "AEGIS", "vettingStatus": "PENDING"}],
        [{"evpId": "AEGIS", "vettingStatus": "UNSCORE"}],
        # Vetted just now: the brand read may lag behind it.
        [
            {
                "evpId": "AEGIS",
                "vettingStatus": "ACTIVE",
                "vettedDate": datetime.now(timezone.utc).isoformat(),
            }
        ],
    ],
)
async def test_a_faster_lane_still_running_waits(vettings) -> None:
    calls: list = []
    p, refs = _bought_lane(calls, vettings)
    st = await p.status(refs)
    assert st.state == "pending" and st.reason is None
    assert "needs_information" not in refs


async def test_an_active_vetting_that_never_verifies_the_brand_holds_the_row() -> None:
    calls: list = []
    day_ago = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    p, refs = _bought_lane(
        calls, [{"evpId": "AEGIS", "vettingStatus": "ACTIVE", "vettedDate": day_ago}]
    )
    assert (await p.status(refs)).state == "pending"
    assert refs["needs_information"] is True


def _held_lane(calls: list, brand: dict):
    """A low-volume row held because its faster lane failed; the brand
    reads ``brand["identity"]`` (the test changes it between passes)."""

    def handler(req: httpx.Request):
        calls.append((req.method, req.url.path))
        if req.method == "GET" and req.url.path.endswith("/10dlc/brand/B1"):
            return httpx.Response(
                200, json={"status": "OK", "identityStatus": brand["identity"]}
            )
        if req.method == "GET" and req.url.path.endswith("/externalVetting"):
            return httpx.Response(
                200, json=[{"evpId": "AEGIS", "vettingStatus": "FAILED"}]
            )
        if req.url.path.endswith("/10dlc/campaignBuilder"):
            return httpx.Response(200, json={"campaignId": "C1"})
        return httpx.Response(404)

    refs = _low_volume_refs(
        faster_lane_ok=True,
        faster_lane_at=datetime.now(timezone.utc).isoformat(),
        vetting_requested=True,
        needs_information=True,
        faster_lane_failed=True,
    )
    return _provider(handler), refs


async def test_a_failed_lane_hold_marks_the_row_to_keep_checking() -> None:
    calls: list = []
    p, refs = _bought_lane(calls, [{"evpId": "AEGIS", "vettingStatus": "FAILED"}])
    await p.status(refs)
    assert refs["needs_information"] is True and refs["faster_lane_failed"] is True


async def test_a_held_lane_still_unverified_stays_held_with_no_request() -> None:
    calls: list = []
    p, refs = _held_lane(calls, {"identity": "UNVERIFIED"})
    before = json.loads(json.dumps(refs))
    st = await p.status(refs)
    assert st.state == "pending" and st.reason == LOW_VOLUME_FASTER_LANE_FAILED
    # Unchanged refs: the poll writes nothing. Only the brand is read.
    assert refs == before
    assert calls == [("GET", "/v2/10dlc/brand/B1")]


async def test_a_held_lane_ends_when_the_brand_is_verified_later() -> None:
    calls: list = []
    brand = {"identity": "VERIFIED"}
    p, refs = _held_lane(calls, brand)
    # The pass that ends the hold files nothing: it got no credit check.
    st = await p.status(refs, may_file_campaign=True)
    assert st.state == "pending" and st.reason is None
    assert "needs_information" not in refs and "faster_lane_failed" not in refs
    assert refs["brand_verified"] is True
    assert p.step(refs) != "needs_information"
    assert not any(c[0] == "POST" for c in calls)
    # A later read that lags behind holds nothing again.
    brand["identity"] = "UNVERIFIED"
    assert (await p.status(refs)).reason is None
    assert "needs_information" not in refs
    brand["identity"] = "VERIFIED"
    # The next pass is a normal one: no campaign without the credit...
    assert (await p.status(refs, may_file_campaign=False)).state == "pending"
    assert "campaign_id" not in refs
    # ...and the campaign once the credit covers it, with no new vetting.
    assert (await p.status(refs, may_file_campaign=True)).state == "pending"
    assert refs["campaign_id"] == "C1"
    posts = [c for c in calls if c[0] == "POST"]
    assert posts == [("POST", "/v2/10dlc/campaignBuilder")]


async def test_no_failed_lane_hold_once_a_campaign_is_filed() -> None:
    calls: list = []
    p, refs = _bought_lane(calls, [{"evpId": "AEGIS", "vettingStatus": "FAILED"}])
    refs["campaign_id"] = "C1"
    assert (await p.status(refs)).state == "pending"
    assert "needs_information" not in refs
    assert not any(c[1].endswith("/externalVetting") for c in calls)
