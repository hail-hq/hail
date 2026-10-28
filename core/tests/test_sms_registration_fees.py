import pytest
from hailhq.core import sms_registration_fees as fees


def test_twilio_business_filing_fee_is_brand_plus_campaign_vetting() -> None:
    # 46.00 brand (with vetting) + 15.00 campaign vetting
    assert fees.filing_fee_cents("twilio", "business") == 6100


def test_twilio_person_filing_fee() -> None:
    # 4.50 brand + 15.00 campaign vetting
    assert fees.filing_fee_cents("twilio", "person") == 1950


def test_monthly_fee() -> None:
    assert fees.monthly_fee_cents("twilio", "business") == 1000
    assert fees.monthly_fee_cents("twilio", "person") == 200


def test_telnyx_fees_match_telnyx() -> None:
    # 4.50 brand + about 41 standard vetting + 15 campaign review
    assert fees.filing_fee_cents("telnyx", "business") == 6050
    # 4.50 brand + 15 campaign review; 2/month for a sole proprietor
    assert fees.filing_fee_cents("telnyx", "person") == 1950
    assert fees.monthly_fee_cents("telnyx", "person") == 200
    assert fees.monthly_fee_cents("telnyx", "business") == 1000


def test_minimum_months() -> None:
    # Telnyx bills campaign fees for three months initially.
    assert fees.minimum_months("telnyx", "person") == 3
    assert fees.minimum_months("telnyx", "business") == 3
    # No field: month by month.
    assert fees.minimum_months("twilio", "business") == 1


def test_unknown_provider_raises() -> None:
    with pytest.raises(KeyError):
        fees.filing_fee_cents("didww", "person")


def test_providers_lists_carriers() -> None:
    assert {"twilio", "telnyx"} <= set(fees.providers())


def test_telnyx_bills_a_refused_brand() -> None:
    for subject in ("person", "business"):
        assert fees.refused_brand_fee_cents("telnyx", subject) == 450


def test_a_carrier_without_a_refused_brand_fee_charges_nothing() -> None:
    assert fees.refused_brand_fee_cents("twilio", "person") == 0
