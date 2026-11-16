import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hailhq.core.config import Settings, settings
from hailhq.core.models import (
    CarrierVerification,
    Organization,
    PhoneNumber,
)
from hailhq.core.sms_trial import (
    CODE_TTL,
    CodeBudgetExceeded,
    CodeExpired,
    NoPendingCode,
    PhoneIsHailNumber,
    PhoneNotUs,
    PhoneTaken,
    WrongCode,
    code_text,
    confirm_phone,
    get_trial_phone,
    is_verified_trial_phone,
    start_phone_proof,
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


PHONE = "+14155550142"
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=UTC)


async def _org(session):
    org_id = uuid.uuid4()
    session.add(Organization(id=org_id, origin="human"))
    await session.flush()
    return org_id


async def _start(session, org, phone=PHONE, now=NOW):
    return await start_phone_proof(
        session, org, phone=phone, consent_version="v1", user_id=None, now=now
    )


def test_code_text():
    assert code_text("123456") == (
        "Your Hail trial code is 123456. It expires in 10 minutes. "
        "Reply STOP to opt out, HELP for help."
    )


async def test_start_issues_six_digit_code_and_stores_hash_only(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    assert re.fullmatch(r"\d{6}", issued.code)
    assert issued.row.code_hash != issued.code
    assert issued.code not in issued.row.code_hash
    assert issued.row.code_expires_at - NOW == CODE_TTL == timedelta(minutes=10)
    assert issued.row.status == "pending"
    assert (await get_trial_phone(async_session, org)).phone_e164 == PHONE


async def test_start_refuses_non_us_phone(async_session):
    org = await _org(async_session)
    with pytest.raises(PhoneNotUs) as e:
        await _start(async_session, org, phone="+447700900123")
    assert str(e.value) == "Trial texts go to US mobile numbers only."


async def test_start_refuses_a_hail_number(async_session):
    org = await _org(async_session)
    other = await _org(async_session)
    async_session.add(
        PhoneNumber(
            id=uuid.uuid4(),
            organization_id=other,
            e164=PHONE,
            country_code="US",
            number_type="local",
            provider="twilio",
            provisioning_state="active",
        )
    )
    await async_session.flush()
    with pytest.raises(PhoneIsHailNumber):
        await _start(async_session, org)


async def test_start_refuses_phone_verified_elsewhere(async_session):
    a, b = await _org(async_session), await _org(async_session)
    issued = await _start(async_session, a)
    await confirm_phone(async_session, a, issued.code, now=NOW)
    with pytest.raises(PhoneTaken):
        await _start(async_session, b)


async def test_sixth_code_in_a_day_is_refused(async_session):
    org = await _org(async_session)
    for _ in range(5):
        await _start(async_session, org)
    with pytest.raises(CodeBudgetExceeded) as e:
        await _start(async_session, org)
    assert str(e.value) == "You asked for 5 codes today. Try again tomorrow."
    assert e.value.retry_after == timedelta(hours=24)
    issued = await _start(async_session, org, now=NOW + timedelta(hours=24))
    assert issued.code is not None
    assert issued.row.code_sends == 1


async def test_same_verified_phone_sends_no_code(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    await confirm_phone(async_session, org, issued.code, now=NOW)
    again = await _start(async_session, org)
    assert again.code is None
    assert again.row.status == "verified"
    assert await is_verified_trial_phone(async_session, org, PHONE)
    assert not await is_verified_trial_phone(async_session, org, "+14155550143")


async def test_new_phone_resets_to_pending(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    await confirm_phone(async_session, org, issued.code, now=NOW)
    new = await _start(async_session, org, phone="+14155550143")
    assert new.code is not None
    assert new.row.status == "pending" and new.row.verified_at is None
    assert not await is_verified_trial_phone(async_session, org, PHONE)
    assert new.row.code_sends == 2


async def test_confirm_right_code_verifies(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    row = await confirm_phone(async_session, org, issued.code, now=NOW)
    assert row.status == "verified" and row.verified_at == NOW
    assert row.code_hash is None and row.code_expires_at is None
    assert row.code_attempts == 0


async def test_confirm_without_start_is_no_pending_code(async_session):
    org = await _org(async_session)
    with pytest.raises(NoPendingCode) as e:
        await confirm_phone(async_session, org, "123456", now=NOW)
    assert str(e.value) == "Send a code first."


async def test_confirm_after_ten_minutes_is_expired(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    with pytest.raises(CodeExpired):
        await confirm_phone(
            async_session,
            org,
            issued.code,
            now=NOW + timedelta(minutes=10, seconds=1),
        )


async def test_fifth_wrong_try_burns_the_code(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    wrong = "000000" if issued.code != "000000" else "111111"
    left = []
    for _ in range(5):
        with pytest.raises(WrongCode) as e:
            await confirm_phone(async_session, org, wrong, now=NOW)
        left.append(e.value.tries_left)
    assert left == [4, 3, 2, 1, 0]
    assert str(e.value) == "The code is not correct. Send a new code."
    with pytest.raises(NoPendingCode):
        await confirm_phone(async_session, org, issued.code, now=NOW)


async def test_wrong_code_message(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    wrong = "000000" if issued.code != "000000" else "111111"
    with pytest.raises(WrongCode) as e:
        await confirm_phone(async_session, org, wrong, now=NOW)
    assert str(e.value) == "The code is not correct. 4 tries left."


async def test_confirm_race_second_workspace_gets_phone_taken(async_session):
    a, b = await _org(async_session), await _org(async_session)
    ia = await _start(async_session, a)
    ib = await _start(async_session, b)
    await confirm_phone(async_session, a, ia.code, now=NOW)
    with pytest.raises(PhoneTaken):
        await confirm_phone(async_session, b, ib.code, now=NOW)
    row_b = await get_trial_phone(async_session, b)
    assert row_b.status == "pending" and row_b.code_hash is not None
    row_b.code_attempts = 1
    await async_session.flush()


async def test_phone_code_cap_spans_workspaces(async_session):
    a, b = await _org(async_session), await _org(async_session)
    for _ in range(3):
        await _start(async_session, a)
    for _ in range(2):
        await _start(async_session, b)
    with pytest.raises(CodeBudgetExceeded) as e:
        await _start(async_session, b)
    assert e.value.retry_after == timedelta(hours=24)
    with pytest.raises(CodeBudgetExceeded):
        await _start(async_session, a)
    other = await _start(async_session, b, phone="+14155550143")
    assert other.code is not None
    later = await _start(async_session, b, now=NOW + timedelta(hours=24))
    assert later.code is not None
