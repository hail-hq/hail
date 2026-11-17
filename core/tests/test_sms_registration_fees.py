import pytest
from hailhq.core import sms_registration_fees as fees


def test_twilio_business_filing_fee_is_brand_plus_campaign_vetting() -> None:
    # 46.00 brand (with vetting) + 15.00 campaign vetting
    assert fees.filing_fee_cents("twilio", "business") == 6100


def test_twilio_person_filing_fee() -> None:
    # 4.50 brand + 15.00 campaign vetting
    assert fees.filing_fee_cents("twilio", "person") == 1950


def test_telnyx_fees_match_telnyx() -> None:
    # 4.50 brand + about 41 standard vetting + 15 campaign review
    assert fees.filing_fee_cents("telnyx", "business") == 6050
    # 4.50 brand + 15 campaign review; 2/month for a sole proprietor
    assert fees.filing_fee_cents("telnyx", "person") == 1950
    assert fees.filing_fee_cents("telnyx", "person", brand_kept=True) == 1500


def test_unknown_provider_raises() -> None:
    with pytest.raises(KeyError):
        fees.filing_fee_cents("didww", "person")


def test_providers_lists_carriers() -> None:
    assert {"twilio", "telnyx"} <= set(fees.providers())


def test_campaign_fee_is_the_campaign_vetting() -> None:
    assert fees.campaign_fee_cents("twilio", "business") == 1500
    assert fees.campaign_fee_cents("telnyx", "person") == 1500
    with pytest.raises(KeyError):
        fees.campaign_fee_cents("didww", "person")


def test_brand_fee_is_the_brand() -> None:
    assert fees.brand_fee_cents("twilio", "business") == 4600
    assert fees.brand_fee_cents("telnyx", "person") == 450


@pytest.mark.parametrize("carrier", ["twilio", "telnyx"])
def test_low_volume_company_pays_the_unvetted_brand(carrier) -> None:
    # 4.50 brand, no vetting + 15.00 campaign review
    assert fees.brand_fee_cents(carrier, "business", low_volume=True) == 450
    assert fees.campaign_fee_cents(carrier, "business", low_volume=True) == 1500
    assert fees.filing_fee_cents(carrier, "business", low_volume=True) == 1950


def test_low_volume_never_changes_a_persons_fees() -> None:
    for carrier in ("twilio", "telnyx"):
        assert fees.filing_fee_cents(
            carrier, "person", low_volume=True
        ) == fees.filing_fee_cents(carrier, "person")


def test_low_volume_rows_in_the_costs_file() -> None:
    rows = {r: fees._load()[r]["business_low_volume"] for r in ("twilio", "telnyx")}
    assert all(row["campaign_monthly_usd"] == "1.50" for row in rows.values())
    # Not yet checked against an invoice, on either carrier.
    assert all(row["verification_method"] == "unverified" for row in rows.values())
    # Telnyx bills its campaign fees three months up front.
    assert rows["telnyx"]["minimum_months"] == 3
    with pytest.raises(KeyError):
        fees.filing_fee_cents("didww", "business", low_volume=True)


def test_the_faster_lane_is_its_own_step() -> None:
    # Telnyx's standard vetting, asked for when the brand is not verified.
    assert fees.faster_lane_fee_cents("telnyx") == 4150
    # Twilio's API cannot add it: no faster lane fee.
    with pytest.raises(KeyError):
        fees.faster_lane_fee_cents("twilio")
    with pytest.raises(KeyError):
        fees.faster_lane_fee_cents("didww")


def test_a_ticked_faster_lane_raises_the_filing_floor_to_the_worst_case() -> None:
    low = fees.filing_fee_cents("telnyx", "business", low_volume=True)
    assert (
        fees.filing_fee_cents("telnyx", "business", low_volume=True, faster_lane=True)
        == low + 4150
    )
    # Nothing to add where the carrier has no faster lane, or no low volume.
    assert fees.filing_fee_cents(
        "twilio", "business", low_volume=True, faster_lane=True
    ) == fees.filing_fee_cents("twilio", "business", low_volume=True)
    assert fees.filing_fee_cents(
        "telnyx", "business", faster_lane=True
    ) == fees.filing_fee_cents("telnyx", "business")
