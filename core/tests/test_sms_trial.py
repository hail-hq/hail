import uuid

import pytest
from hailhq.core.config import Settings, settings
from hailhq.core.models import CarrierVerification, Organization, PhoneNumber
from hailhq.core.sms_trial import (
    TRIAL_FOOTER,
    TRIAL_PREFIX,
    trial_body,
    trial_enabled,
    trial_number,
    trial_sender,
)
from pydantic import ValidationError

TRIAL_E164 = "+18005550199"


def test_trial_body_wraps_prefix_and_footer():
    assert (
        trial_body("hi", limit=1600) == "[Hail trial] hi\nHail does not read replies."
    )


def test_trial_body_refuses_when_too_long():
    overhead = len(TRIAL_PREFIX) + len(TRIAL_FOOTER)
    assert trial_body("x" * (1600 - overhead), limit=1600) is not None
    assert trial_body("x" * (1601 - overhead), limit=1600) is None


def test_trial_body_truncate_keeps_footer():
    out = trial_body("x" * 2000, limit=480, truncate=True)
    assert len(out) == 480 and out.startswith(TRIAL_PREFIX)
    assert out.endswith(TRIAL_FOOTER)


def test_setting_rejects_non_e164():
    with pytest.raises(ValidationError):
        Settings(hail_sms_trial_number="18005550199")
    assert Settings(hail_sms_trial_number="").hail_sms_trial_number == ""
    assert Settings(hail_sms_trial_number=TRIAL_E164).hail_sms_trial_number


def test_trial_enabled_needs_number_and_sending(monkeypatch):
    monkeypatch.setattr(settings, "hail_sms_trial_number", "")
    monkeypatch.setattr(settings, "hail_sms_trial_sending", True)
    assert not trial_enabled()
    monkeypatch.setattr(settings, "hail_sms_trial_number", TRIAL_E164)
    monkeypatch.setattr(settings, "hail_sms_trial_sending", False)
    assert not trial_enabled()
    monkeypatch.setattr(settings, "hail_sms_trial_sending", True)
    assert trial_enabled()


async def _seed(session, *, state="active", capabilities=("sms",)):
    org_id = uuid.uuid4()
    session.add(Organization(id=org_id, origin="human"))
    await session.flush()
    number = PhoneNumber(
        id=uuid.uuid4(),
        organization_id=org_id,
        e164=TRIAL_E164,
        country_code="US",
        number_type="toll_free",
        capabilities=list(capabilities),
        provider="twilio",
        provisioning_state=state,
    )
    session.add(number)
    await session.flush()
    return org_id, number


async def _approve(session, org_id, number):
    reg = CarrierVerification(
        id=uuid.uuid4(),
        organization_id=org_id,
        number_id=number.id,
        provider="twilio",
        country_code="US",
        number_type="toll_free",
        subject_type="business",
        purpose="sms",
        state="approved",
        requirements_version="test",
    )
    session.add(reg)
    await session.flush()
    number.sms_registration_id = reg.id
    await session.flush()


@pytest.mark.asyncio
async def test_trial_number_off_when_setting_empty(async_session, monkeypatch):
    await _seed(async_session)
    monkeypatch.setattr(settings, "hail_sms_trial_number", "")
    assert await trial_number(async_session) is None


@pytest.mark.asyncio
async def test_trial_number_finds_active_sms_row(async_session, monkeypatch):
    await _seed(async_session)
    monkeypatch.setattr(settings, "hail_sms_trial_number", TRIAL_E164)
    assert (await trial_number(async_session)).e164 == TRIAL_E164


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "state,capabilities", [("released", ("sms",)), ("active", ("voice",))]
)
async def test_trial_number_ignores_inactive_or_voice_only(
    async_session, monkeypatch, state, capabilities
):
    await _seed(async_session, state=state, capabilities=capabilities)
    monkeypatch.setattr(settings, "hail_sms_trial_number", TRIAL_E164)
    assert await trial_number(async_session) is None


@pytest.mark.asyncio
async def test_trial_sender_needs_an_approved_registration(async_session, monkeypatch):
    org_id, number = await _seed(async_session)
    monkeypatch.setattr(settings, "hail_sms_trial_number", TRIAL_E164)
    monkeypatch.setattr(settings, "hail_sms_trial_sending", True)
    assert await trial_number(async_session) is not None
    assert await trial_sender(async_session) is None

    await _approve(async_session, org_id, number)
    monkeypatch.setattr(settings, "hail_sms_trial_sending", False)
    assert await trial_sender(async_session) is None

    monkeypatch.setattr(settings, "hail_sms_trial_sending", True)
    assert (await trial_sender(async_session)).id == number.id
