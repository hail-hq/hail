import asyncio
import re
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from hailhq.core import sms_trial
from hailhq.core.config import Settings
from hailhq.core.db import org_lock
from hailhq.core.models import (
    Organization,
    PhoneNumber,
    Sms,
    SmsTrialPhone,
)
from hailhq.core.providers.sms.base import SmsProvider
from hailhq.core.providers.sms.telnyx import TelnyxSmsProvider
from hailhq.core.providers.sms.twilio import TwilioSmsProvider
from hailhq.core.sms_trial import (
    APPROVED_AT_KEY,
    CODE_TTL,
    PAUSED_KEY,
    SMS_TRIAL_LIMIT,
    TRIAL_FOOTER,
    TRIAL_PREFIX,
    CodeBudgetExceeded,
    CodeExpired,
    NoPendingCode,
    PhoneIsHailNumber,
    PhoneNotUs,
    PhoneTaken,
    TrialBodyTooLong,
    TrialSend,
    WrongCode,
    claim_trial_send,
    code_text,
    confirm_phone,
    confirms_tollfree_approval,
    get_trial_phone,
    is_verified_trial_phone,
    start_phone_proof,
    trial_body,
    trial_configured,
    trial_hint_applies,
    trial_refusal_reason,
    trial_sender,
    trial_texts_used,
)
from sqlalchemy import event, text
from sqlalchemy.exc import IntegrityError

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


def test_no_trial_settings_left():
    """Trial numbers live in the database: no env var picks or turns them on."""
    assert not [f for f in Settings.model_fields if "sms_trial" in f]


async def _seed(
    session,
    *,
    state="active",
    capabilities=("sms",),
    e164=TRIAL_E164,
    approved=False,
    marked=True,
    created_at=None,
):
    """A Hail pool toll-free number, marked for trial texts by default."""
    number = PhoneNumber(
        id=uuid.uuid4(),
        organization_id=None,
        is_pool=True,
        pool_purpose="sms_trial" if marked else None,
        e164=e164,
        country_code="US",
        number_type="toll_free",
        capabilities=list(capabilities),
        provider="twilio",
        provider_resource_id=f"PN{e164[-4:]}",
        provisioning_state=state,
        provisioning_metadata=(
            {APPROVED_AT_KEY: datetime.now(UTC).isoformat()} if approved else {}
        ),
        **({"created_at": created_at} if created_at else {}),
    )
    session.add(number)
    await session.flush()
    return number


async def _approve(session, number):
    number.provisioning_metadata = {APPROVED_AT_KEY: datetime.now(UTC).isoformat()}
    await session.flush()


async def test_trial_configured_needs_a_marked_number(async_session):
    await _seed(async_session, marked=False)
    assert await trial_configured(async_session) is False
    await _seed(async_session, e164="+18005550198")
    assert await trial_configured(async_session) is True


@pytest.mark.parametrize(
    "state,capabilities", [("released", ("sms",)), ("active", ("voice",))]
)
async def test_trial_numbers_ignore_inactive_or_voice_only(
    async_session, state, capabilities
):
    await _seed(async_session, state=state, capabilities=capabilities, approved=True)
    assert await trial_configured(async_session) is False
    assert await trial_sender(async_session) is None


async def test_trial_sender_needs_the_cached_approval(async_session):
    number = await _seed(async_session)
    assert await trial_configured(async_session) is True
    assert await trial_sender(async_session) is None
    await _approve(async_session, number)
    assert (await trial_sender(async_session)).id == number.id


async def test_trial_sender_is_the_oldest_ready_number(async_session):
    old = await _seed(
        async_session, approved=True, created_at=datetime(2026, 1, 1, tzinfo=UTC)
    )
    new = await _seed(
        async_session,
        e164="+18005550198",
        approved=True,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    # Adding a newer number keeps the sender, so codes and texts come from
    # the same number.
    assert (await trial_sender(async_session)).id == old.id
    old.provisioning_metadata = {}
    await async_session.flush()
    assert (await trial_sender(async_session)).id == new.id


async def test_trial_sender_never_uses_a_call_pool_number(async_session):
    await _seed(async_session, marked=False, approved=True)
    assert await trial_sender(async_session) is None
    assert await trial_configured(async_session) is False


@pytest.mark.parametrize("change", ["owned", "local", "not_pool"])
async def test_only_a_toll_free_pool_number_can_be_marked(async_session, change):
    """The database refuses a mark on a workspace's own number (so a
    workspace never trial-texts from a number it owns) or a local number."""
    org = await _org(async_session)
    fields = {
        "owned": {"organization_id": org, "is_pool": False},
        "local": {"number_type": "local"},
        "not_pool": {"organization_id": org, "is_pool": False},
    }[change]
    row = {
        "organization_id": None,
        "is_pool": True,
        "number_type": "toll_free",
        **fields,
    }
    async_session.add(
        PhoneNumber(
            pool_purpose="sms_trial",
            e164="+18005550197",
            country_code="US",
            capabilities=["sms"],
            provider="twilio",
            provisioning_state="active",
            **row,
        )
    )
    with pytest.raises(IntegrityError):
        await async_session.flush()


PHONE = "+14155550142"
OTHER = "+14155550143"
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
    assert str(e.value) == (
        "Your workspace asked for 5 codes in the last 24 hours. Try again later."
    )
    assert e.value.retry_after == timedelta(hours=24)
    issued = await _start(async_session, org, now=NOW + timedelta(hours=24))
    assert issued.code is not None
    assert len(issued.row.code_send_log) == 1


async def test_same_verified_phone_sends_no_code(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    await confirm_phone(async_session, org, issued.code, now=NOW)
    again = await _start(async_session, org)
    assert again.code is None
    assert again.row.status == "verified"
    assert await is_verified_trial_phone(async_session, org, PHONE)
    assert not await is_verified_trial_phone(async_session, org, "+14155550143")


async def test_new_phone_keeps_the_verified_phone_until_confirmed(async_session):
    """A new code goes to the new phone; the verified phone keeps getting
    trial texts until the new one is confirmed, so a typo cannot end the
    trial."""
    org = await _org(async_session)
    issued = await _start(async_session, org)
    verified = await confirm_phone(async_session, org, issued.code, now=NOW)
    verified_at = verified.verified_at
    new = await _start(async_session, org, phone=OTHER)
    assert new.code is not None and new.phone == OTHER
    assert new.row.status == "verified" and new.row.verified_at == verified_at
    assert new.row.phone_e164 == PHONE and new.row.pending_phone_e164 == OTHER
    assert await is_verified_trial_phone(async_session, org, PHONE)
    assert not await is_verified_trial_phone(async_session, org, OTHER)
    assert len(new.row.code_send_log) == 2
    assert new.row.code_send_log[-1]["phone"] == OTHER

    later = NOW + timedelta(minutes=1)
    row = await confirm_phone(async_session, org, new.code, now=later)
    assert row.status == "verified" and row.verified_at == later
    assert row.phone_e164 == OTHER and row.pending_phone_e164 is None
    assert await is_verified_trial_phone(async_session, org, OTHER)
    assert not await is_verified_trial_phone(async_session, org, PHONE)


async def test_burned_switch_code_keeps_the_verified_phone(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    await confirm_phone(async_session, org, issued.code, now=NOW)
    new = await _start(async_session, org, phone=OTHER)
    wrong = "000000" if new.code != "000000" else "111111"
    for _ in range(5):
        with pytest.raises(WrongCode):
            await confirm_phone(async_session, org, wrong, now=NOW)
    row = await get_trial_phone(async_session, org)
    assert row.pending_phone_e164 is None and row.code_hash is None
    assert row.status == "verified" and row.phone_e164 == PHONE
    assert await is_verified_trial_phone(async_session, org, PHONE)


async def test_switch_to_a_phone_taken_meanwhile_keeps_the_verified_phone(
    async_session,
):
    a, b = await _org(async_session), await _org(async_session)
    first = await _start(async_session, a)
    await confirm_phone(async_session, a, first.code, now=NOW)
    switch = await _start(async_session, a, phone=OTHER)
    other = await _start(async_session, b, phone=OTHER)
    await confirm_phone(async_session, b, other.code, now=NOW)
    with pytest.raises(PhoneTaken):
        await confirm_phone(async_session, a, switch.code, now=NOW)
    assert await is_verified_trial_phone(async_session, a, PHONE)
    assert not await is_verified_trial_phone(async_session, a, OTHER)


async def test_new_pending_phone_replaces_the_unconfirmed_one(async_session):
    org = await _org(async_session)
    await _start(async_session, org)
    new = await _start(async_session, org, phone=OTHER)
    assert new.phone == OTHER
    assert new.row.status == "pending" and new.row.phone_e164 == OTHER
    assert new.row.pending_phone_e164 is None


async def test_confirm_right_code_verifies(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    row = await confirm_phone(async_session, org, issued.code, now=NOW)
    assert row.status == "verified" and row.verified_at == NOW
    assert row.code_hash is None and row.code_expires_at is None
    assert row.code_attempts == 0


async def test_confirm_sets_updated_at(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org)
    later = NOW + timedelta(minutes=1)
    wrong = "000000" if issued.code != "000000" else "111111"
    with pytest.raises(WrongCode):
        await confirm_phone(async_session, org, wrong, now=later)
    row = await get_trial_phone(async_session, org)
    assert row.updated_at == later
    latest = NOW + timedelta(minutes=2)
    row = await confirm_phone(async_session, org, issued.code, now=latest)
    assert row.updated_at == latest


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
    assert str(e.value) == (
        "This phone got the maximum of 5 codes in the last 24 hours. "
        "Try again later."
    )
    with pytest.raises(CodeBudgetExceeded):
        await _start(async_session, a)
    other = await _start(async_session, b, phone="+14155550143")
    assert other.code is not None
    later = await _start(async_session, b, now=NOW + timedelta(hours=24))
    assert later.code is not None


async def test_rotation_bypass_is_closed(async_session):
    a, b = await _org(async_session), await _org(async_session)
    for _ in range(4):
        await _start(async_session, a)
    await _start(async_session, a, phone="+14155550144")  # A moves to Q
    assert (await get_trial_phone(async_session, a)).phone_e164 == "+14155550144"
    # The 4 codes A sent to PHONE still count: B gets one more, then is refused.
    assert (await _start(async_session, b)).code is not None
    with pytest.raises(CodeBudgetExceeded):
        await _start(async_session, b)


async def test_rolling_window(async_session):
    org = await _org(async_session)
    for h in (0, 5, 10, 15, 20):
        await _start(async_session, org, now=NOW + timedelta(hours=h))
    with pytest.raises(CodeBudgetExceeded) as e:
        await _start(async_session, org, now=NOW + timedelta(hours=23))
    assert e.value.retry_after == timedelta(hours=1)
    again = await _start(async_session, org, now=NOW + timedelta(hours=24, seconds=1))
    assert again.code is not None
    assert len(again.row.code_send_log) == 5


# --- claim_trial_send ---


async def _verify(session, org, phone=PHONE, status="verified"):
    session.add(
        SmsTrialPhone(
            organization_id=org,
            phone_e164=phone,
            status=status,
            consent_version="v1",
            consent_at=NOW,
            verified_at=NOW if status == "verified" else None,
        )
    )
    await session.flush()


async def _trial_texts(session, org, n, *, status="sent"):
    for _ in range(n):
        session.add(
            Sms(
                organization_id=org,
                from_e164=TRIAL_E164,
                to_e164=PHONE,
                direction="outbound",
                status=status,
                body="[Hail trial] hi\nHail does not read replies.",
                metadata_={"sms_trial": True},
            )
        )
    await session.flush()


async def _ready(session, *, approve=True):
    """A working baseline: a ready trial number, a customer workspace with a
    verified phone and no trial texts used."""
    number = await _seed(session, approved=approve)
    org = await _org(session)
    await _verify(session, org)
    return number, org


async def _claim(session, org, **kw):
    kw.setdefault("to", PHONE)
    kw.setdefault("body", "hi")
    kw.setdefault("limit", 1600)
    return await claim_trial_send(session, org, **kw)


@pytest.mark.parametrize(
    "case",
    [
        "unmarked",
        "other_from",
        "call_pool_from",
        "not_ready_from",
        "not_us",
        "not_verified_phone",
        "pending_phone",
        "other_workspace_phone",
        "number_not_approved",
        "cap_used",
    ],
)
async def test_claim_refuses(case, async_session):
    number, org = await _ready(async_session, approve=case != "number_not_approved")
    kw = {}
    if case == "unmarked":
        number.pool_purpose = None
        await async_session.flush()
    elif case == "other_from":
        kw["explicit_from"] = "+18005550198"
    elif case == "call_pool_from":
        await _seed(async_session, e164="+18005550198", marked=False, approved=True)
        kw["explicit_from"] = "+18005550198"
    elif case == "not_ready_from":
        await _seed(async_session, e164="+18005550198")
        kw["explicit_from"] = "+18005550198"
    elif case == "not_us":
        kw["to"] = "+447700900123"
    elif case == "not_verified_phone":
        kw["to"] = "+14155550143"
    elif case == "pending_phone":
        row = await get_trial_phone(async_session, org)
        row.status = "pending"
        await async_session.flush()
    elif case == "other_workspace_phone":
        other = await _org(async_session)
        await _verify(async_session, other, phone="+14155550144")
        kw["to"] = "+14155550144"
    elif case == "cap_used":
        await _trial_texts(async_session, org, SMS_TRIAL_LIMIT)
    assert await _claim(async_session, org, **kw) is None


async def test_claim_allows_and_wraps(async_session):
    number, org = await _ready(async_session)
    t = await _claim(async_session, org)
    assert isinstance(t, TrialSend)
    assert t.number.id == number.id and t.number.e164 == "+18005550199"
    assert t.body == "[Hail trial] hi\nHail does not read replies."
    assert t.metadata == {"sms_trial": True}
    assert await _claim(async_session, org, explicit_from=TRIAL_E164) is not None


async def test_claim_sends_from_the_named_ready_trial_number(async_session):
    first, org = await _ready(async_session)
    second = await _seed(
        async_session,
        e164="+18005550198",
        approved=True,
        created_at=datetime(2099, 1, 1, tzinfo=UTC),
    )
    assert (await _claim(async_session, org)).number.id == first.id
    named = await _claim(async_session, org, explicit_from=second.e164)
    assert named.number.id == second.id


async def test_claim_body_too_long_raises(async_session):
    _, org = await _ready(async_session)
    max_chars = 160 - len(TRIAL_PREFIX) - len(TRIAL_FOOTER)
    assert await _claim(async_session, org, body="x" * max_chars, limit=160)
    with pytest.raises(TrialBodyTooLong) as e:
        await _claim(async_session, org, body="x" * (max_chars + 1), limit=160)
    assert e.value.max_chars == max_chars
    assert str(e.value) == (
        f"Trial texts can have up to {max_chars} characters. Nothing was sent."
    )
    cut = await _claim(
        async_session, org, body="x" * (max_chars + 1), limit=160, truncate=True
    )
    assert len(cut.body) == 160 and cut.body.endswith(TRIAL_FOOTER)


async def test_failed_rows_do_not_use_the_cap(async_session):
    _, org = await _ready(async_session)
    await _trial_texts(async_session, org, 24)
    await _trial_texts(async_session, org, 3, status="failed")
    async_session.add(
        Sms(
            organization_id=org,
            from_e164=PHONE,
            to_e164=TRIAL_E164,
            direction="inbound",
            status="received",
            body="hi",
            metadata_={"sms_trial": True},
        )
    )
    async_session.add(
        Sms(
            organization_id=org,
            from_e164="+18005550198",
            to_e164=PHONE,
            body="not a trial text",
        )
    )
    await async_session.flush()
    assert await trial_texts_used(async_session, org) == 24
    assert await _claim(async_session, org) is not None


async def test_25th_ok_26th_refused(async_session):
    _, org = await _ready(async_session)
    await _trial_texts(async_session, org, 24)
    assert await _claim(async_session, org) is not None
    await _trial_texts(async_session, org, 1)
    assert await trial_texts_used(async_session, org) == 25
    assert await _claim(async_session, org) is None


async def test_two_concurrent_claims_at_24_send_one(async_session, session_factory):
    _, org = await _ready(async_session)
    await _trial_texts(async_session, org, 24)
    await async_session.commit()

    async def claim_and_send() -> TrialSend | None:
        async with session_factory() as s:
            t = await _claim(s, org)
            if t is None:
                await s.rollback()
                return None
            # Give the other session time to run its own count. Without the
            # lock both would see 24 here and both would send.
            await asyncio.sleep(0.2)
            s.add(
                Sms(
                    organization_id=org,
                    from_e164=t.number.e164,
                    to_e164=PHONE,
                    body=t.body,
                    metadata_={**t.metadata},
                )
            )
            await s.commit()
            return t

    results = await asyncio.wait_for(
        asyncio.gather(claim_and_send(), claim_and_send()), timeout=10
    )
    assert sum(r is not None for r in results) == 1
    async with session_factory() as fresh:
        assert await trial_texts_used(fresh, org) == 25


async def test_claim_checks_the_phone_under_the_lock(async_session, session_factory):
    """A phone switch that commits while a claim waits on the workspace
    lock stops that claim: the phone is checked again under the lock."""
    _, org = await _ready(async_session)
    await async_session.commit()

    async with session_factory() as holder, session_factory() as claimer:
        await org_lock(holder, org)
        claim = asyncio.create_task(_claim(claimer, org))
        await asyncio.sleep(0.3)
        assert not claim.done()  # past the first phone check, waiting on the lock
        row = await get_trial_phone(holder, org)
        row.status = "pending"
        await holder.commit()
        assert await asyncio.wait_for(claim, timeout=10) is None
        await claimer.rollback()


async def test_start_stores_the_canonical_phone(async_session):
    org = await _org(async_session)
    issued = await _start(async_session, org, phone="+1 415 555 0142\n")
    assert issued.row.phone_e164 == PHONE
    assert issued.row.code_send_log[-1]["phone"] == PHONE


async def test_non_canonical_phone_cannot_skip_the_taken_check(async_session):
    a, b = await _org(async_session), await _org(async_session)
    issued = await _start(async_session, a)
    await confirm_phone(async_session, a, issued.code, now=NOW)
    with pytest.raises(PhoneTaken):
        await _start(async_session, b, phone=PHONE + "\n")


async def test_non_canonical_phone_counts_toward_the_phone_cap(async_session):
    a, b = await _org(async_session), await _org(async_session)
    for _ in range(5):
        await _start(async_session, a)
    with pytest.raises(CodeBudgetExceeded):
        await _start(async_session, b, phone=PHONE + "\n")


@pytest.mark.parametrize("bad", ["+1abc", "not a phone", "", "+12345", "+1415555014"])
async def test_unparsable_phone_is_not_us(async_session, bad):
    org = await _org(async_session)
    with pytest.raises(PhoneNotUs):
        await _start(async_session, org, phone=bad)


@pytest.mark.parametrize("case", ["unmarked", "cap_used", "not_approved"])
async def test_trial_hint_applies_only_when_usable(async_session, case):
    number, org = await _ready(async_session, approve=case != "not_approved")
    if case == "unmarked":
        number.pool_purpose = None
        await async_session.flush()
    elif case == "cap_used":
        await _trial_texts(async_session, org, SMS_TRIAL_LIMIT)
    assert await trial_hint_applies(async_session, org) is False


async def test_trial_hint_applies_with_texts_left(async_session):
    _, org = await _ready(async_session)
    await _trial_texts(async_session, org, SMS_TRIAL_LIMIT - 1)
    assert await trial_hint_applies(async_session, org) is True


async def test_claim_keeps_the_verified_phone_during_a_switch(
    async_session,
):
    _, org = await _ready(async_session)
    await _start(async_session, org, phone=OTHER)
    assert await _claim(async_session, org) is not None
    assert await _claim(async_session, org, to=OTHER) is None


async def test_asking_again_for_the_verified_phone_drops_the_pending_one(
    async_session,
):
    org = await _org(async_session)
    first = await _start(async_session, org)
    await confirm_phone(async_session, org, first.code, now=NOW)
    switch = await _start(async_session, org, phone=OTHER)
    later = NOW + timedelta(minutes=1)
    again = await _start(async_session, org, now=later)
    assert again.code is None
    row = again.row
    assert row.status == "verified" and row.phone_e164 == PHONE
    assert row.pending_phone_e164 is None
    assert row.code_hash is None and row.code_expires_at is None
    assert row.code_attempts == 0 and row.updated_at == later
    with pytest.raises(NoPendingCode):
        await confirm_phone(async_session, org, switch.code, now=later)


# --- trial_refusal_reason ---


@pytest.mark.parametrize(
    "case,reason",
    [
        ("unmarked", "Trial texting is not available."),
        ("number_not_approved", "Trial texting is not available."),
        ("number_missing", "Trial texting is not available."),
        ("not_us", "Trial texts go only to your verified trial phone."),
        ("not_verified_phone", "Trial texts go only to your verified trial phone."),
        ("pending_phone", "Trial texts go only to your verified trial phone."),
        ("cap_used", "No trial texts left."),
        ("usable", None),
    ],
)
async def test_trial_refusal_reason(async_session, case, reason):
    number, org = await _ready(async_session, approve=case != "number_not_approved")
    to = PHONE
    if case == "unmarked":
        number.pool_purpose = None
        await async_session.flush()
    elif case == "number_missing":
        number.provisioning_state = "released"
        await async_session.flush()
    elif case == "not_us":
        to = "+447700900123"
    elif case == "not_verified_phone":
        to = OTHER
    elif case == "pending_phone":
        row = await get_trial_phone(async_session, org)
        row.status = "pending"
        await async_session.flush()
    elif case == "cap_used":
        await _trial_texts(async_session, org, SMS_TRIAL_LIMIT)
    assert (
        await trial_refusal_reason(async_session, org, to=to, explicit_from=TRIAL_E164)
        == reason
    )


# --- trial_texts_used and the partial index ---


async def test_trial_texts_used_sends_the_index_predicate_as_a_literal(
    async_session,
):
    """The partial index ``sms_trial_org_idx`` has the predicate
    ``metadata ? 'sms_trial'``. A key sent as a bind parameter cannot match
    it in a generic prepared plan, so the query must carry the literal."""
    _, org = await _ready(async_session)
    seen: list[tuple[str, object]] = []

    def _capture(_conn, _cursor, statement, parameters, _context, _many):
        seen.append((statement, parameters))

    engine = async_session.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _capture)
    try:
        await trial_texts_used(async_session, org)
    finally:
        event.remove(engine, "before_cursor_execute", _capture)
    statement, parameters = next((s, p) for s, p in seen if "count(" in s)
    assert "metadata ? 'sms_trial'" in statement
    assert "sms_trial" not in repr(parameters)

    # A generic plan (what a reused prepared statement gets) uses the index.
    conn = await async_session.connection()
    await conn.exec_driver_sql("SET LOCAL plan_cache_mode = force_generic_plan")
    await conn.exec_driver_sql("SET LOCAL enable_seqscan = off")
    await conn.exec_driver_sql(f"PREPARE trial_used_q AS {statement}")
    try:
        args = ", ".join(f"'{a}'" for a in parameters)
        plan = "\n".join(
            r[0]
            for r in (
                await conn.execute(text(f"EXPLAIN EXECUTE trial_used_q({args})"))
            ).all()
        )
    finally:
        await conn.exec_driver_sql("DEALLOCATE trial_used_q")
    assert "sms_trial_org_idx" in plan, plan


# --- refresh_trial_approvals: the cached carrier approval ---


class _Carrier:
    def __init__(self, answer):
        self.answer = answer
        self.asked: list[str] = []

    async def tollfree_approved(self, *, provider_resource_id, e164, fresh=False):
        assert fresh, "the refresh must skip the in-process cache"
        self.asked.append(e164)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


@pytest.mark.parametrize(
    "before,answer,after",
    [
        (False, True, True),
        (True, False, False),
        # No answer (lookup failed, carrier error) keeps the last answer, so
        # a slow carrier never turns the trial off.
        (True, None, True),
        (False, None, False),
        (True, RuntimeError("carrier down"), True),
    ],
)
async def test_refresh_caches_the_carrier_answer(
    async_session, monkeypatch, before, answer, after
):
    number = await _seed(async_session, approved=before)
    await async_session.commit()
    carrier = _Carrier(answer)
    monkeypatch.setattr(sms_trial, "sms_route", lambda provider: carrier)
    await sms_trial.refresh_trial_approvals(async_session)
    assert carrier.asked == [TRIAL_E164]
    await async_session.refresh(number)
    assert (APPROVED_AT_KEY in number.provisioning_metadata) is after
    assert (await trial_sender(async_session) is not None) is after


async def test_refresh_asks_only_about_marked_numbers(async_session, monkeypatch):
    await _seed(async_session, marked=False, e164="+18005550198")
    await _seed(async_session)
    await async_session.commit()
    carrier = _Carrier(True)
    monkeypatch.setattr(sms_trial, "sms_route", lambda provider: carrier)
    counts = await sms_trial.refresh_trial_approvals(async_session)
    assert carrier.asked == [TRIAL_E164]
    assert counts == {"approved": 1, "not_approved": 0, "no_answer": 0, "changed": 1}


async def test_requests_never_ask_the_carrier(async_session, monkeypatch):
    """Readiness is read from the row: GET, hints and claims make no
    carrier call."""

    def no_carrier(provider):
        raise AssertionError("carrier called on a request")

    monkeypatch.setattr(sms_trial, "sms_route", no_carrier)
    _, org = await _ready(async_session)
    assert await trial_configured(async_session)
    assert await trial_hint_applies(async_session, org)
    assert await _claim(async_session, org) is not None


@pytest.mark.parametrize(
    "stamp,ready",
    [(timedelta(hours=1), True), (timedelta(hours=25), False)],
)
async def test_an_old_approval_is_not_ready(async_session, stamp, ready):
    """Lookups that keep failing keep the last yes: after 24 hours without
    a fresh yes the number is not ready."""
    number = await _seed(async_session)
    number.provisioning_metadata = {
        APPROVED_AT_KEY: (datetime.now(UTC) - stamp).isoformat()
    }
    await async_session.flush()
    assert (await trial_sender(async_session) is not None) is ready


async def test_a_paused_number_is_not_ready(async_session):
    number = await _seed(async_session, approved=True)
    number.provisioning_metadata = {
        **number.provisioning_metadata,
        PAUSED_KEY: datetime.now(UTC).isoformat(),
    }
    await async_session.flush()
    assert await trial_sender(async_session) is None
    assert await trial_configured(async_session)


@pytest.mark.parametrize(
    "before,answer,changed",
    [(False, True, 1), (True, True, 0), (True, False, 1), (True, None, 0)],
)
async def test_refresh_counts_only_changed_answers(
    async_session, monkeypatch, before, answer, changed
):
    await _seed(async_session, approved=before)
    await async_session.commit()
    monkeypatch.setattr(sms_trial, "sms_route", lambda provider: _Carrier(answer))
    counts = await sms_trial.refresh_trial_approvals(async_session)
    assert counts["changed"] == changed


def test_only_a_carrier_that_answers_can_confirm_approval():
    assert confirms_tollfree_approval("twilio") is True
    assert confirms_tollfree_approval("telnyx") is False
    assert confirms_tollfree_approval("no-such-carrier") is False
    # The list matches the adapters that answer the check.
    base = SmsProvider.tollfree_approved
    assert TwilioSmsProvider.tollfree_approved is not base
    assert TelnyxSmsProvider.tollfree_approved is base
