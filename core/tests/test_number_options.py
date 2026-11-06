"""number_options: one row per number type with per-subject best option."""

import json
from uuid import uuid4

import pytest
from hailhq.core import number_options as mod
from hailhq.core import telephony_catalog
from hailhq.core.carrier_offer import OfferVerification
from hailhq.core.number_options import number_options, option_key

CH_PERSON = {
    "accepted": True,
    "items": ["id_document", "birth_date", "personal_details"],
}
CH_BUSINESS = {
    "accepted": True,
    "items": ["company_name", "company_id", "company_registration"],
}


def row(cc="CH", nt="local", usd="1.00", **kw):
    base = {
        "country_code": cc,
        "number_type": nt,
        "usd_per_month": usd,
        "voice": True,
        "sms": False,
        "mms": False,
        "verification_required": False,
    }
    return {**base, **kw}


def need(person=CH_PERSON, business=CH_BUSINESS):
    return {
        "verification_required": True,
        "requirements": {"person": person, "business": business},
    }


@pytest.fixture
def catalog(tmp_path, monkeypatch):
    def write(**by_provider):
        for name in ("twilio", "telnyx", "didww"):
            (tmp_path / f"{name}.json").write_text(
                json.dumps(
                    {
                        "version": 3,
                        "numbers": by_provider.get(name, []),
                        "capability_monthly_fees": [
                            {"capabilities": ["sms", "mms"], "usd_per_month": "0.10"}
                        ],
                    }
                )
            )
        telephony_catalog._load.cache_clear()

    monkeypatch.setenv("HAIL_TELEPHONY_CATALOG_DIR", str(tmp_path))
    monkeypatch.setattr(mod, "get_verification_provider", lambda name: object())
    yield write
    telephony_catalog._load.cache_clear()


def ov(state, subject="person", name="Ann"):
    return OfferVerification(
        id=uuid4(), state=state, subject_type=subject, subject_name=name
    )


def one(r, nt="local", subject="person"):
    (t,) = [t for t in r.types if t.number_type == nt]
    return t.for_[subject]


def test_us_no_paperwork(catalog):
    catalog(twilio=[row("US")])
    r = number_options("US", {})
    assert r.needs_paperwork is False
    o = one(r)
    assert o.status == "ready" and o.paperwork == [] and o.paperwork_known


def test_best_option_ready_beats_cheaper(catalog):
    catalog(
        twilio=[row(usd="1.00", **need())],
        telnyx=[row(usd="5.00", **need())],
    )
    v = ov("approved")
    r = number_options("CH", {("telnyx", "CH", "local"): [v]})
    o = one(r)
    assert o.status == "ready" and o.monthly_cents == 500
    assert o.verification.id == v.id and o.more_options == 1


def test_company_only_type_for_person(catalog):
    catalog(
        twilio=[
            row(
                usd="2.00",
                **need(person={"accepted": False, "items": []}),
            )
        ]
    )
    r = number_options("CH", {})
    p, b = one(r), one(r, subject="business")
    assert p.allowed is False and p.reason == "company_only"
    assert p.monthly_cents == 200 and p.paperwork == b.paperwork
    assert b.allowed is True and b.reason is None


def test_person_only_reason(catalog):
    catalog(twilio=[row(**need(business={"accepted": False, "items": []}))])
    b = one(number_options("CH", {}), subject="business")
    assert b.allowed is False and b.reason == "person_only"


def test_by_request_after_buy_at_same_status(catalog):
    catalog(
        twilio=[row(usd="3.00", **need())],
        didww=[row(usd="1.00", by_request=True, **need())],
    )
    o = one(number_options("CH", {}))
    assert o.how == "buy" and o.monthly_cents == 300 and o.more_options == 1


def test_buy_after_check_beats_by_request_without_papers(catalog):
    # UK, company: support orders a DIDWW number with no papers, Twilio sells
    # one online after a check. The online one is shown.
    catalog(
        twilio=[row(cc="GB", nt="mobile", usd="2.50", sms=True, **need())],
        didww=[row(cc="GB", nt="mobile", usd="2.50", by_request=True)],
    )
    r = number_options("GB", {})
    o = r.types[0].for_["business"]
    assert o.how == "buy" and o.status == "needs_check"
    assert "sms" in o.capabilities and o.more_options == 1


def test_fewer_items_then_price(catalog):
    few = need(person={"accepted": True, "items": ["id_document"]})
    catalog(
        twilio=[row(usd="1.00", **need())],
        telnyx=[
            row(usd="9.00", **few),
        ],
        didww=[],
    )
    assert one(number_options("CH", {})).monthly_cents == 900
    catalog(
        twilio=[row(usd="4.00", **need())],
        telnyx=[row(usd="2.00", setup_usd="1.00", **need())],
    )
    o = one(number_options("CH", {}))
    assert (o.monthly_cents, o.setup_cents) == (200, 100)


def test_monthly_includes_sms_fee(catalog):
    catalog(telnyx=[row("US", usd="1.00", sms=True)])
    o = one(number_options("US", {}))
    assert o.monthly_cents == 110 and o.capabilities == ["voice", "sms"]


def test_capabilities_sms_filters_options(catalog):
    catalog(
        telnyx=[row("US", usd="1.00", sms=True)],
        twilio=[row("US", usd="0.50")],
    )
    assert one(number_options("US", {}, capabilities=["sms"])).monthly_cents == 110
    catalog(twilio=[row("US")])
    assert number_options("US", {}, capabilities=["sms"]).types == []


def test_capabilities_voice_filters_options(catalog):
    catalog(
        telnyx=[row("US", usd="1.00", sms=True)],
        twilio=[row("US", usd="0.50")],
    )
    assert one(number_options("US", {}, capabilities=["voice"])).monthly_cents == 50
    catalog(twilio=[row("US", voice=False, sms=True)])
    assert number_options("US", {}, capabilities=["voice"]).types == []


def test_capabilities_voice_and_sms_need_both(catalog):
    catalog(
        telnyx=[row("US", usd="1.00", sms=True)],
        twilio=[row("US", usd="0.50")],
    )
    both = ["voice", "sms"]
    assert one(number_options("US", {}, capabilities=both)).monthly_cents == 110
    catalog(twilio=[row("US", voice=False, sms=True)])
    assert number_options("US", {}, capabilities=both).types == []


AU_FEW = need(
    person={"accepted": True, "items": ["id_document"]},
    business={"accepted": True, "items": ["company_name"]},
)


def test_default_prefers_option_with_every_capability(catalog):
    catalog(
        didww=[row("AU", "mobile", usd="8.00", sms=True, **AU_FEW)],
        telnyx=[row("AU", "mobile", usd="8.00", sms=True, **need())],
    )
    # didww has fewer papers but routes no SMS: voice only.
    o = one(number_options("AU", {}), "mobile", "business")
    assert o.capabilities == ["voice", "sms"] and o.more_options == 1
    v = one(number_options("AU", {}, capabilities=["voice"]), "mobile", "business")
    assert v.capabilities == ["voice"] and v.more_options == 1


def _mixed(catalog, voice_only, both):
    catalog(
        didww=[row("AU", "mobile", usd="8.00", **voice_only)],
        telnyx=[row("AU", "mobile", usd="8.00", sms=True, **both)],
    )
    return one(number_options("AU", {}), "mobile", "person")


def test_ready_voice_only_beats_needs_check_voice_and_sms(catalog):
    o = _mixed(catalog, need(), need())
    assert o.capabilities == ["voice", "sms"]
    ready = {("didww", "AU", "mobile"): [ov("approved")]}
    r = number_options("AU", ready)
    assert one(r, "mobile", "person").capabilities == ["voice"]


def test_buy_voice_only_beats_by_request_voice_and_sms(catalog):
    catalog(
        didww=[row("AU", "mobile", usd="8.00")],
        telnyx=[row("AU", "mobile", usd="8.00", sms=True, by_request=True)],
    )
    o = one(number_options("AU", {}), "mobile", "person")
    assert o.capabilities == ["voice"] and o.how == "buy"


def test_voice_filter_picks_voice_only(catalog):
    o = _mixed(catalog, {}, {})
    assert o.capabilities == ["voice", "sms"]
    v = one(number_options("AU", {}, capabilities=["voice"]), "mobile", "person")
    assert v.capabilities == ["voice"] and v.monthly_cents == 800


def test_person_and_business_share_one_union(catalog):
    # twilio accepts only business, with SMS; telnyx accepts both, voice only.
    catalog(
        twilio=[
            row(
                "AU",
                "mobile",
                usd="8.00",
                sms=True,
                **need(person={"accepted": False, "items": []}, business=CH_BUSINESS),
            )
        ],
        telnyx=[row("AU", "mobile", usd="9.00")],
    )
    ready = {("twilio", "AU", "mobile"): [ov("approved", "business")]}
    r = number_options("AU", ready)
    # One union {voice, sms} for the type: the business cell prefers its
    # voice+sms option; the person cell only has the voice-only one.
    assert one(r, "mobile", "person").capabilities == ["voice"]
    assert one(r, "mobile", "business").capabilities == ["voice", "sms"]


def test_default_disjoint_capabilities_voice_only_beats_sms_only(catalog):
    catalog(
        twilio=[row("US", usd="1.00", voice=False, sms=True)],
        telnyx=[row("US", usd="2.00")],
    )
    o = one(number_options("US", {}))
    assert o.capabilities == ["voice"] and o.more_options == 1


def test_missing_requirements_block(catalog):
    catalog(twilio=[row(verification_required=True)])
    r = number_options("CH", {})
    assert r.needs_paperwork is True
    for s in ("person", "business"):
        o = one(r, subject=s)
        assert o.allowed and o.paperwork == [] and o.paperwork_known is False
        assert o.status == "needs_check"


def test_unconfigured_carrier_skipped(catalog, monkeypatch):
    catalog(
        twilio=[row(usd="1.00", **need())],
        telnyx=[row(usd="9.00", **need()), row("US")],
    )
    monkeypatch.setattr(
        mod,
        "get_verification_provider",
        lambda name: None if name == "twilio" else object(),
    )
    assert one(number_options("CH", {})).monthly_cents == 900
    # a row that needs no verification stays even when unconfigured
    monkeypatch.setattr(mod, "get_verification_provider", lambda name: None)
    assert one(number_options("US", {})).status == "ready"


def test_in_review_status(catalog):
    catalog(twilio=[row(**need())])
    v = ov("submitted", name="Acme AG", subject="business")
    r = number_options("CH", {("twilio", "CH", "local"): [v]})
    b = one(r, subject="business")
    assert b.status == "in_review" and b.verification.subject_name == "Acme AG"
    assert b.verification.subject_type == "business"
    assert one(r).status == "needs_check" and one(r).verification is None


def test_more_options_count(catalog):
    catalog(
        twilio=[row(**need())],
        telnyx=[row(**need())],
        didww=[row(by_request=True, **need())],
    )
    assert one(number_options("CH", {})).more_options == 2


def test_sms_route_none_is_voice_only(catalog):
    catalog(didww=[row("US", sms=True)])
    assert one(number_options("US", {})).capabilities == ["voice"]


def test_by_request_ready_with_any_approval(catalog):
    catalog(didww=[row(by_request=True, **need())])
    v = ov("approved", subject="business", name="Acme")
    r = number_options("CH", {("didww", "CH", "local"): [v]})
    o = one(r)
    assert o.how == "by_request" and o.status == "ready"
    assert o.verification.id == v.id


def test_option_key_order():
    # Buy online beats a support order, even one that needs no papers.
    assert option_key("needs_check", "buy", 0, 9, 900, 0) < option_key(
        "ready", "by_request", 0, 0, 1, 0
    )
    assert option_key("ready", "buy", 0, 9, 900, 0) < option_key(
        "in_review", "buy", 0, 0, 1, 0
    )
    assert option_key("ready", "buy", 0, 3, 1, 0) < option_key(
        "ready", "by_request", 0, 0, 1, 0
    )
    assert option_key("ready", "buy", 0, 3, 1, 0) < option_key(
        "ready", "buy", 1, 0, 1, 0
    )
    assert option_key("ready", "buy", 1, 1, 500, 0) < option_key(
        "ready", "buy", 1, 2, 1, 0
    )
    assert option_key("ready", "buy", 0, 1, 100, 50) < option_key(
        "ready", "buy", 0, 1, 200, 0
    )


def test_no_carrier_name_in_json(catalog):
    catalog(twilio=[row(**need())])
    text = number_options("CH", {}).model_dump_json(by_alias=True)
    assert '"for"' in text and "twilio" not in text.lower()


def test_by_request_row_without_verification_provider_skipped(catalog, monkeypatch):
    catalog(
        didww=[row("US", by_request=True)],
        twilio=[row("US", usd="5.00")],
    )
    monkeypatch.setattr(
        mod,
        "get_verification_provider",
        lambda name: None if name == "didww" else object(),
    )
    o = one(number_options("US", {}))
    assert o.how == "buy" and o.more_options == 0


def test_disallowed_copy_has_no_more_options_or_verification(catalog):
    catalog(
        twilio=[
            row(**need(person={"accepted": False, "items": []})),
        ],
        telnyx=[row(usd="2.00", **need(person={"accepted": False, "items": []}))],
    )
    v = ov("approved", subject="business", name="Acme")
    r = number_options("CH", {("twilio", "CH", "local"): [v]})
    b, p = one(r, subject="business"), one(r)
    assert b.more_options == 1 and b.verification is not None
    assert p.allowed is False
    assert p.more_options == 0 and p.verification is None


def test_unknown_paperwork_ranks_after_known(catalog):
    catalog(
        twilio=[row(usd="1.00", verification_required=True)],
        telnyx=[row(usd="9.00", **need())],
    )
    o = one(number_options("CH", {}))
    assert o.paperwork_known is True and o.monthly_cents == 900
    assert option_key("needs_check", "buy", 0, 0, 1, 0, False) > option_key(
        "needs_check", "buy", 0, 5, 1, 0
    )


def test_needs_paperwork_follows_verification_required(catalog):
    empty = {"accepted": True, "items": []}
    catalog(twilio=[row(**need(person=empty, business=empty))])
    assert number_options("CH", {}).needs_paperwork is True


@pytest.mark.parametrize("state", ["draft", "rejected", "cancelled"])
def test_not_sent_or_over_verification_is_not_in_review(catalog, state):
    catalog(twilio=[row(**need())])
    v = ov(state, name="Acme AG", subject="business")
    r = number_options("CH", {("twilio", "CH", "local"): [v]})
    b = one(r, subject="business")
    assert b.status == "needs_check" and b.verification is None


@pytest.mark.parametrize("state", ["awaiting_review", "submitting", "submitted"])
def test_submitted_states_count_as_in_review(catalog, state):
    catalog(twilio=[row(**need())])
    v = ov(state, name="Acme AG", subject="business")
    r = number_options("CH", {("twilio", "CH", "local"): [v]})
    assert one(r, subject="business").status == "in_review"


def test_disallowed_copy_status_is_needs_check(catalog):
    catalog(twilio=[row(**need(person={"accepted": False, "items": []}))])
    v = ov("approved", subject="business", name="Acme")
    r = number_options("CH", {("twilio", "CH", "local"): [v]})
    assert one(r, subject="business").status == "ready"
    p = one(r)
    assert p.allowed is False
    assert p.status == "needs_check" and p.verification is None
    assert p.more_options == 0


def test_subject_accepted_follows_catalog_block():
    block = {"person": {"accepted": False, "items": []}, "business": CH_BUSINESS}
    assert mod.subject_accepted(block, "person") is False
    assert mod.subject_accepted(block, "business") is True
    # No block: unknown, kept.
    assert mod.subject_accepted(None, "person") is True


def test_offer_paperwork_returns_accepted_flag(catalog):
    catalog(twilio=[row(**need(person={"accepted": False, "items": []}))])
    p = mod.offer_paperwork("CH", "local", "twilio", "person", True)
    assert p.known is True and p.accepted is False
    assert mod.offer_paperwork("CH", "local", "twilio", "business", True).accepted
    none = mod.offer_paperwork("CH", "local", "twilio", "person", False)
    assert (none.items, none.known, none.accepted) == ([], True, True)


def test_paperwork_from_block():
    assert mod.paperwork_from_block(None, "person", True) == ([], False, True)
    got = mod.paperwork_from_block({"person": CH_PERSON}, "person", True)
    assert got.known and got.accepted
    assert [i.key for i in got.items] == [
        "id_document",
        "birth_date",
        "personal_details",
    ]


def test_union_counts_buy_options_only_when_any_buy(catalog):
    # The by-request row has sms; the buy-now rows do not. The union is
    # {voice}, so the cheaper buy option is not penalised for lacking sms.
    catalog(
        didww=[row("AU", "mobile", usd="5.00")],
        telnyx=[row("AU", "mobile", usd="9.00", sms=True, by_request=True)],
    )
    o = one(number_options("AU", {}), "mobile", "person")
    assert o.how == "buy" and o.monthly_cents == 500
