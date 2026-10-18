from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from hailhq.core.providers.verification.base import Address
from hailhq.core.providers.verification.twilio_sms import TwilioSmsRegistrationProvider


class _Node:
    def __init__(self, root, path):
        self._root = root
        self._path = path

    def __getattr__(self, name):
        return _Node(self._root, [*self._path, name])

    def __call__(self, *args, **kwargs):
        if args and not kwargs:  # resource(sid) style
            return _Node(self._root, [*self._path, f"({args[0]})"])
        return self._root.record(self._path, kwargs)


class Recorder:
    """A fake twilio.rest.Client: every attribute path records calls and
    returns canned objects from ``responses``, matched by the longest key
    that ends the dotted path (resource ``(sid)`` segments are ignored)."""

    def __init__(self, responses: dict):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses

    def __getattr__(self, name):
        return _Node(self, [name])

    def record(self, path, kwargs):
        name = ".".join(path)
        self.calls.append((name, kwargs))
        plain = ".".join(p for p in path if not p.startswith("("))
        keys = [k for k in self.responses if plain == k or plain.endswith("." + k)]
        if not keys:
            return None
        resp = self.responses[max(keys, key=len)]
        return resp() if callable(resp) else resp


def _ok_responses():
    return {
        "customer_profiles.create": SimpleNamespace(sid="BU1"),
        "end_users.create": SimpleNamespace(sid="IT1"),
        "addresses.create": SimpleNamespace(sid="AD1"),
        "supporting_documents.create": SimpleNamespace(sid="RD1"),
        "customer_profiles_entity_assignments.create": SimpleNamespace(sid="BV1"),
        "customer_profiles_evaluations.create": SimpleNamespace(
            status="compliant", results=[]
        ),
        "customer_profiles.update": SimpleNamespace(sid="BU1", status="pending-review"),
        "trust_products.create": SimpleNamespace(sid="BU2"),
        "trust_products_entity_assignments.create": SimpleNamespace(sid="BV2"),
        "trust_products_evaluations.create": SimpleNamespace(
            status="compliant", results=[]
        ),
        "trust_products.update": SimpleNamespace(sid="BU2", status="pending-review"),
        "brand_registrations.create": SimpleNamespace(
            sid="BN1", status="PENDING", identity_status="UNVERIFIED"
        ),
        "services.create": SimpleNamespace(sid="MG1"),
    }


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


class FakeSms:
    """The Twilio SMS adapter: records service and attach calls."""

    def __init__(self, attach_error=None):
        self.ensured: list[dict] = []
        self.attached: list[tuple[str, str]] = []
        self.attach_error = attach_error

    async def ensure_messaging_service(self, organization_id, existing_sid, name=None):
        self.ensured.append(
            {"org": organization_id, "existing_sid": existing_sid, "name": name}
        )
        return existing_sid or ("MG2" if name else "MG1")

    async def attach_number(self, messaging_service_sid, provider_resource_id):
        if self.attach_error is not None:
            raise self.attach_error
        self.attached.append((messaging_service_sid, provider_resource_id))


def _provider(responses=None, sms=None):
    return TwilioSmsRegistrationProvider(
        account_sid="AC1",
        auth_token="t",
        client=Recorder(responses or _ok_responses()),
        primary_profile_sid="BU0",
        sms_provider=sms or FakeSms(),
    )


async def test_business_draft_creates_profile_trust_and_brand() -> None:
    p = _provider()
    req = await p.requirements("US", "local", "business")
    draft = await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=FIELDS,
        address=ADDRESS,
        documents={},
        messaging_service_sid="MG9",
    )
    assert draft.problems == []
    assert draft.refs["profile_sid"] == "BU1"
    assert draft.refs["trust_sid"] == "BU2"
    assert draft.refs["brand_sid"] == "BN1"
    assert draft.refs["messaging_service_sid"] == "MG9"
    assert draft.refs["subject_type"] == "business"
    names = [n for n, _ in p._client.calls]
    assert "trusthub.v1.customer_profiles.create" in names
    brand = next(
        kw for n, kw in p._client.calls if n.endswith("brand_registrations.create")
    )
    assert brand["customer_profile_bundle_sid"] == "BU1"
    assert brand["a2p_profile_bundle_sid"] == "BU2"
    assert "brand_type" not in brand
    # No personal data in refs
    # (the fixed campaign text may name the sender; the other refs are IDs)
    ids = repr({k: v for k, v in draft.refs.items() if k != "campaign"})
    assert "Acme" not in ids and "12-3456789" not in repr(draft.refs)


async def test_person_draft_uses_sole_proprietor_and_sends_otp() -> None:
    p = _provider()
    fields = {
        **FIELDS,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "mobile_phone_number": "+15125550199",
    }
    req = await p.requirements("US", "local", "person")
    draft = await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=fields,
        address=ADDRESS,
        documents={},
        messaging_service_sid=None,
    )
    brand = next(
        kw for n, kw in p._client.calls if n.endswith("brand_registrations.create")
    )
    assert brand["brand_type"] == "SOLE_PROPRIETOR"
    # Twilio texts the code when the brand is approved; none is asked for here.
    assert not any(
        n.endswith("brand_registration_otps.create") for n, _ in p._client.calls
    )
    assert "otp_sent_at" not in draft.refs
    # A person's campaign gets its own service, made by the SMS adapter.
    assert draft.refs["messaging_service_sid"] == "MG2"
    assert p._sms.ensured == [
        {"org": "org1", "existing_sid": None, "name": "hail-org-org1-person"}
    ]
    assert not _calls(p, "services.create")


async def test_person_draft_refuses_hail_number_as_mobile(monkeypatch) -> None:
    p = _provider()
    monkeypatch.setattr(p, "_is_hail_number", lambda e164: e164 == "+15125550199")
    fields = {
        **FIELDS,
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "mobile_phone_number": "+15125550199",
    }
    req = await p.requirements("US", "local", "person")
    draft = await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=fields,
        address=ADDRESS,
        documents={},
        messaging_service_sid=None,
    )
    assert [pr.field for pr in draft.problems] == ["mobile_phone_number"]
    assert p._client.calls == []


def _campaign(use_case="MIXED"):
    return {
        "use_case": use_case,
        "description": "d" * 40,
        "message_flow": "f" * 40,
        "samples": ["s" * 20, "t" * 20],
        "has_links": False,
        "privacy_policy_url": "https://acme.example/privacy",
        "terms_url": "https://acme.example/terms",
    }


async def test_status_pending_while_brand_pending() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="PENDING", identity_status="UNVERIFIED", brand_feedback=None
    )
    p = _provider(r)
    st = await p.status(
        {
            "brand_sid": "BN1",
            "messaging_service_sid": "MG1",
            "subject_type": "business",
            "campaign": _campaign(),
        }
    )
    assert st.state == "pending"


async def test_status_creates_campaign_once_brand_approved() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="VERIFIED", brand_feedback=None
    )
    r["us_app_to_person.create"] = SimpleNamespace(
        sid="QE1", campaign_status="PENDING", errors=[]
    )
    p = _provider(r)
    refs = {
        "brand_sid": "BN1",
        "messaging_service_sid": "MG1",
        "subject_type": "business",
        "campaign": _campaign(),
    }
    st = await p.status(refs)
    assert st.state == "pending"
    assert refs["campaign_sid"] == "QE1"
    create = next(
        kw for n, kw in p._client.calls if n.endswith("us_app_to_person.create")
    )
    assert create["us_app_to_person_usecase"] == "MIXED"
    assert create["privacy_policy_url"] == "https://acme.example/privacy"
    assert create["terms_and_conditions_url"] == "https://acme.example/terms"


async def test_status_files_an_old_snapshot_without_links() -> None:
    """A draft filed before the links existed still reaches the carrier."""
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(status="APPROVED")
    r["us_app_to_person.create"] = SimpleNamespace(
        sid="QE1", campaign_status="PENDING", errors=[]
    )
    p = _provider(r)
    old = _campaign()
    old.pop("privacy_policy_url")
    old.pop("terms_url")
    refs = {
        "subject_type": "business",
        "brand_sid": "BN1",
        "messaging_service_sid": "MG1",
        "campaign": old,
    }
    assert (await p.status(refs)).state == "pending"
    create = next(
        kw for n, kw in p._client.calls if n.endswith("us_app_to_person.create")
    )
    assert "privacy_policy_url" not in create
    assert create["brand_registration_sid"] == "BN1"


async def test_status_person_waits_for_otp_before_campaign() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="UNVERIFIED", brand_feedback=None
    )
    p = _provider(r)
    refs = {
        "brand_sid": "BN1",
        "messaging_service_sid": "MG1",
        "subject_type": "person",
        "campaign": _campaign("SOLE_PROPRIETOR"),
    }
    st = await p.status(refs)
    assert st.state == "pending" and "campaign_sid" not in refs


async def test_status_approved_when_campaign_verified() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="VERIFIED", brand_feedback=None
    )
    r["us_app_to_person.fetch"] = SimpleNamespace(
        sid="QE1", campaign_status="VERIFIED", errors=[]
    )
    p = _provider(r)
    st = await p.status(
        {
            "brand_sid": "BN1",
            "messaging_service_sid": "MG1",
            "campaign_sid": "QE1",
            "subject_type": "business",
            "campaign": {},
        }
    )
    assert st.state == "approved"


async def test_status_rejected_carries_carrier_reason() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="VERIFIED", brand_feedback=None
    )
    r["us_app_to_person.fetch"] = SimpleNamespace(
        sid="QE1",
        campaign_status="FAILED",
        errors=[
            {"description": "Website does not contain sufficient business information."}
        ],
    )
    p = _provider(r)
    st = await p.status(
        {
            "brand_sid": "BN1",
            "messaging_service_sid": "MG1",
            "campaign_sid": "QE1",
            "subject_type": "business",
            "campaign": {},
        }
    )
    assert st.state == "rejected"
    assert "sufficient business information" in st.reason


def _raise(exc):
    def _f():
        raise exc

    return _f


def _tw(status, msg="boom", code=None):
    from twilio.base.exceptions import TwilioRestException

    return TwilioRestException(status, "u", msg=msg, code=code)


def _calls(p, suffix):
    return [kw for n, kw in p._client.calls if n.endswith(suffix)]


async def _draft(p, subject="business", **extra):
    fields = {**FIELDS, **extra}
    if subject == "person":
        fields.update(
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            mobile_phone_number="+15125550199",
        )
    req = await p.requirements("US", "local", subject)
    return await p.create_draft(
        organization_id="org1",
        contact_email="ops@hail.so",
        requirements=req,
        fields=fields,
        address=ADDRESS,
        documents={},
        messaging_service_sid="MG9",
    )


async def test_business_draft_policies_end_users_and_updates() -> None:
    p = _provider()
    await _draft(p)
    profile = _calls(p, "customer_profiles.create")[0]
    assert profile["policy_sid"] == "RNdfbf3fae0e1107f8aded0e7cead80bf5"
    trust = _calls(p, "trust_products.create")[0]
    assert trust["policy_sid"] == "RNb0d4771c2c98518d916a3d4cd70a8f8b"
    users = {kw["type"]: kw["attributes"] for kw in _calls(p, "end_users.create")}
    assert set(users) == {
        "customer_profile_business_information",
        "authorized_representative_1",
        "us_a2p_messaging_profile_information",
    }
    assert {"business_name", "business_registration_number", "website_url"} <= set(
        users["customer_profile_business_information"]
    )
    assert {"first_name", "last_name", "email", "phone_number"} <= set(
        users["authorized_representative_1"]
    )
    assert set(users["us_a2p_messaging_profile_information"]) == {"company_type"}
    updates = [kw for n, kw in p._client.calls if n.endswith(".update")]
    assert updates == [{"status": "pending-review"}] * 2


async def test_person_draft_policies_and_sole_proprietor_attributes() -> None:
    p = _provider()
    await _draft(p, "person")
    assert _calls(p, "customer_profiles.create")[0]["policy_sid"] == (
        "RN806dd6cd175f314e1f96a9727ee271f4"
    )
    assert _calls(p, "trust_products.create")[0]["policy_sid"] == (
        "RN670d5d2e282a6130ae063b234b6019c8"
    )
    users = {u["type"]: u["attributes"] for u in _calls(p, "end_users.create")}
    assert set(users) == {
        "starter_customer_profile_information",
        "sole_proprietor_information",
    }
    assert set(users["starter_customer_profile_information"]) == {
        "first_name",
        "last_name",
        "email",
        "phone_number",
    }
    assert set(users["sole_proprietor_information"]) == {
        "brand_name",
        "mobile_phone_number",
        "vertical",
    }
    # The starter profile gets the address and Hail's primary profile, is
    # evaluated against the starter policy, and is submitted.
    assert _calls(p, "addresses.create")
    assigned = [
        kw["object_sid"]
        for kw in _calls(p, "customer_profiles_entity_assignments.create")
    ]
    assert assigned == ["IT1", "RD1", "BU0"]
    assert _calls(p, "customer_profiles_evaluations.create")[0]["policy_sid"] == (
        "RN806dd6cd175f314e1f96a9727ee271f4"
    )
    updates = [kw for n, kw in p._client.calls if n.endswith(".update")]
    assert updates == [{"status": "pending-review"}] * 2


async def test_discard_order_and_404_does_not_stop_next() -> None:
    r = _ok_responses()
    r["us_app_to_person.delete"] = _raise(_tw(404))
    r["trust_products.delete"] = _raise(_tw(500))
    r["customer_profiles.delete"] = True
    p = _provider(r)
    await p.discard(
        {
            "profile_sid": "BU1",
            "trust_sid": "BU2",
            "brand_sid": "BN1",
            "messaging_service_sid": "MG1",
            "campaign_sid": "QE1",
        }
    )
    names = [n for n, _ in p._client.calls]
    assert names == [
        "messaging.v1.services.(MG1).us_app_to_person.(QE1).delete",
        "trusthub.v1.trust_products.(BU2).delete",
        "trusthub.v1.customer_profiles.(BU1).delete",
    ]


async def test_discard_never_raises_on_other_errors() -> None:
    r = _ok_responses()
    r["trust_products.delete"] = _raise(RuntimeError("x"))
    r["customer_profiles.delete"] = True
    p = _provider(r)
    await p.discard({"profile_sid": "BU1", "trust_sid": "BU2", "brand_sid": "BN1"})
    assert len(p._client.calls) == 2


async def test_draft_cleans_up_on_non_twilio_error() -> None:
    from hailhq.core.providers.verification.base import VerificationProviderError

    r = _ok_responses()
    r["brand_registrations.create"] = _raise(RuntimeError("x"))
    r["trust_products.delete"] = True
    r["customer_profiles.delete"] = True
    p = _provider(r)
    with pytest.raises(VerificationProviderError):
        await _draft(p)
    names = [n for n, _ in p._client.calls if n.endswith(".delete")]
    assert names == [
        "trusthub.v1.trust_products.(BU2).delete",
        "trusthub.v1.customer_profiles.(BU1).delete",
    ]


async def test_bad_link_is_a_problem_and_sends_nothing() -> None:
    p = _provider()
    draft = await _draft(p, privacy_policy_url="acme.example/privacy")
    assert [pr.field for pr in draft.problems] == ["privacy_policy_url"]
    assert draft.refs == {} and p._client.calls == []


async def test_draft_4xx_becomes_neutral_problem() -> None:
    r = _ok_responses()
    r["brand_registrations.create"] = _raise(_tw(400, "EIN 12-3456789 not found"))
    r["trust_products.delete"] = True
    r["customer_profiles.delete"] = True
    draft = await _draft(_provider(r))
    assert draft.refs == {}
    assert [pr.message for pr in draft.problems] == ["Check the tax ID (EIN)."]
    assert "12-3456789" not in repr(draft.problems)


async def test_draft_4xx_unknown_message_is_generic() -> None:
    r = _ok_responses()
    r["brand_registrations.create"] = _raise(_tw(400, "Twilio internal thing"))
    draft = await _draft(_provider(r))
    assert draft.problems[0].message.startswith("The carrier refused a detail")


async def test_draft_5xx_raises_provider_error() -> None:
    from hailhq.core.providers.verification.base import VerificationProviderError

    r = _ok_responses()
    r["brand_registrations.create"] = _raise(_tw(503))
    with pytest.raises(VerificationProviderError):
        await _draft(_provider(r))


async def test_draft_noncompliant_evaluation_is_problem() -> None:
    r = _ok_responses()
    r["customer_profiles_evaluations.create"] = SimpleNamespace(
        status="noncompliant",
        results=[{"fields": [{"passed": False, "failure_reason": "bad website"}]}],
    )
    draft = await _draft(_provider(r))
    assert [pr.message for pr in draft.problems] == ["Check the website address."]


@pytest.mark.parametrize("brand_status", ["FAILED", "SUSPENDED"])
async def test_status_brand_failed_or_suspended_is_rejected(brand_status) -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status=brand_status, identity_status="UNVERIFIED", brand_feedback=None
    )
    st = await _provider(r).status(
        {"brand_sid": "BN1", "messaging_service_sid": "MG1", "campaign": {}}
    )
    assert st.state == "rejected" and st.reason


async def test_status_campaign_failed_without_errors_has_generic_reason() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="VERIFIED", brand_feedback=None
    )
    r["us_app_to_person.fetch"] = SimpleNamespace(
        sid="QE1", campaign_status="FAILED", errors=[]
    )
    st = await _provider(r).status(
        {
            "brand_sid": "BN1",
            "messaging_service_sid": "MG1",
            "campaign_sid": "QE1",
            "subject_type": "business",
            "campaign": {},
        }
    )
    assert st.state == "rejected" and st.reason


async def test_business_draft_uses_the_org_service() -> None:
    p = _provider()
    draft = await _draft(p)
    assert draft.refs["messaging_service_sid"] == "MG9"
    assert p._sms.ensured == [{"org": "org1", "existing_sid": "MG9", "name": None}]


async def test_attach_number_puts_the_number_in_the_service() -> None:
    p = _provider()
    await p.attach_number(
        {"messaging_service_sid": "MG1", "subject_type": "business"},
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert p._sms.attached == [("MG1", "PN1")]
    assert not _calls(p, "phone_numbers.list")


async def test_attach_number_refused_by_carrier_raises() -> None:
    from hailhq.core.providers.sms.base import SmsProvisioningError
    from hailhq.core.providers.verification.base import VerificationProviderError

    p = _provider(sms=FakeSms(attach_error=SmsProvisioningError("no")))
    with pytest.raises(VerificationProviderError):
        await p.attach_number(
            {"messaging_service_sid": "MG1", "subject_type": "business"},
            provider_resource_id="PN1",
            e164="+15125550100",
        )


async def test_attach_number_person_holds_one_number(caplog) -> None:
    from hailhq.core.providers.verification.base import VerificationProviderError

    r = _ok_responses()
    r["phone_numbers.list"] = [SimpleNamespace(sid="PN0")]
    p = _provider(r)
    with pytest.raises(VerificationProviderError):
        await p.attach_number(
            {"messaging_service_sid": "MG2", "subject_type": "person"},
            provider_resource_id="PN1",
            e164="+15125550100",
        )
    assert p._sms.attached == []
    assert "+1512" not in caplog.text


@pytest.mark.parametrize("present", [[], [SimpleNamespace(sid="PN1")]])
async def test_attach_number_person_empty_or_same_number(present) -> None:
    r = _ok_responses()
    r["phone_numbers.list"] = present
    p = _provider(r)
    await p.attach_number(
        {"messaging_service_sid": "MG2", "subject_type": "person"},
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert p._sms.attached == [("MG2", "PN1")]


async def test_detach_person_number_leaves_the_person_service() -> None:
    r = _ok_responses()
    r["phone_numbers.delete"] = _raise(_tw(404))
    p = _provider(r)
    left = await p.detach_number(
        {"messaging_service_sid": "MG2", "subject_type": "person"},
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert left is True
    assert [n for n, _ in p._client.calls] == [
        "messaging.v1.services.(MG2).phone_numbers.(PN1).delete"
    ]


async def test_detach_business_number_stays_in_the_org_service() -> None:
    p = _provider()
    left = await p.detach_number(
        {"messaging_service_sid": "MG1", "subject_type": "business"},
        provider_resource_id="PN1",
        e164="+15125550100",
    )
    assert left is False
    assert p._client.calls == []


def test_needs_the_primary_profile_sid(monkeypatch) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "twilio_primary_profile_sid", "")
    with pytest.raises(ValueError, match="TWILIO_PRIMARY_PROFILE_SID"):
        TwilioSmsRegistrationProvider(
            account_sid="AC1", auth_token="t", client=Recorder(_ok_responses())
        )


async def test_business_draft_assigns_the_primary_profile() -> None:
    p = _provider()
    await _draft(p)
    assigned = [kw["object_sid"] for kw in _calls(p, "entity_assignments.create")]
    assert "BU0" in assigned


async def test_person_first_poll_after_approval_sends_no_code() -> None:
    # Twilio texts the first code when the brand is approved.
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="UNVERIFIED", brand_feedback=None
    )
    p = _provider(r)
    refs = {
        "brand_sid": "BN1",
        "messaging_service_sid": "MG2",
        "subject_type": "person",
        "campaign": _campaign("SOLE_PROPRIETOR"),
    }
    st = await p.status(refs)
    assert st.state == "pending"
    assert not _calls(p, "brand_registration_otps.create")
    assert refs["otp_sent_at"]


@pytest.mark.parametrize(("age_hours", "sends"), [(25, 1), (1, 0)])
async def test_person_otp_is_sent_again_after_a_day(age_hours, sends) -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="UNVERIFIED", brand_feedback=None
    )
    p = _provider(r)
    sent_at = (datetime.now(timezone.utc) - timedelta(hours=age_hours)).isoformat()
    refs = {
        "brand_sid": "BN1",
        "messaging_service_sid": "MG1",
        "subject_type": "person",
        "campaign": _campaign("SOLE_PROPRIETOR"),
        "otp_sent_at": sent_at,
    }
    st = await p.status(refs)
    assert st.state == "pending"
    assert len(_calls(p, "brand_registration_otps.create")) == sends
    if sends:
        assert refs["otp_sent_at"] > sent_at
    else:
        assert refs["otp_sent_at"] == sent_at


async def test_confirmation_is_reply_until_identity_verified() -> None:
    p = _provider()
    refs = {"subject_type": "person", "brand_sid": "BN1"}
    assert p.confirmation(refs) == "reply"
    refs["identity_verified"] = True
    assert p.confirmation(refs) is None
    assert p.confirmation({"subject_type": "business"}) is None


async def test_confirm_by_code_is_not_supported() -> None:
    with pytest.raises(NotImplementedError):
        await _provider().confirm({"subject_type": "person"}, "123456")


async def test_status_marks_person_identity_verified() -> None:
    r = _ok_responses()
    r["brand_registrations.fetch"] = SimpleNamespace(
        status="APPROVED", identity_status="VERIFIED", brand_feedback=None
    )
    r["us_app_to_person.create"] = SimpleNamespace(
        sid="QE1", campaign_status="PENDING", errors=[]
    )
    refs = {
        "brand_sid": "BN1",
        "messaging_service_sid": "MG1",
        "subject_type": "person",
        "campaign": _campaign("SOLE_PROPRIETOR"),
    }
    assert (await _provider(r).status(refs)).state == "pending"
    assert refs["identity_verified"] is True and refs["campaign_sid"] == "QE1"
