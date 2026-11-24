"""Connected number helpers: credentials, number shape, connection lookup."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core.config import settings
from hailhq.core.models import Call, CallEvent, PhoneNumber, SipConnection
from hailhq.core.schemas import PBX_CHECK_METADATA_PREFIX
from hailhq.core.secret_cipher import SecretKeyMissing, generate_key
from hailhq.core.sip_connections import (
    CHECK_CALL_META,
    CHECK_CODE_DIGITS,
    CHECK_CODE_META,
    CHECK_EVENT_KIND,
    CHECK_WINDOW,
    carrier_caller,
    check_code_matches,
    check_in_flight,
    check_outcomes,
    connection_of,
    decrypt_secret,
    encrypt_secret,
    hash_check_code,
    new_inbound_username,
    new_password,
    normalize_caller,
    number_shape,
)

T0 = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
ORG = uuid.uuid4()


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())


def test_password_is_long_enough():
    assert len(new_password()) >= 24
    assert new_password() != new_password()


def test_username_format():
    assert re.fullmatch(r"hail-[0-9a-f]{12}", new_inbound_username())


@pytest.mark.parametrize(
    "e164,shape",
    [
        ("+14155550100", ("US", "local")),
        ("+442079460000", ("GB", "local")),
        ("+447400123456", ("GB", "mobile")),
        ("+18005550100", ("US", "toll_free")),
    ],
)
def test_number_shape(e164, shape):
    assert number_shape(e164) == shape


def test_number_shape_refuses_invalid():
    with pytest.raises(ValueError):
        number_shape("+1415")


def test_secret_round_trip(monkeypatch):
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    assert decrypt_secret(encrypt_secret("s3cret")) == "s3cret"


@pytest.fixture
async def conn(async_session):
    row = SipConnection(
        organization_id=ORG,
        label="Main office",
        inbound_username=new_inbound_username(),
        encrypted_inbound_password="x",
        default_region="US",
    )
    async_session.add(row)
    await async_session.flush()
    return row


def _number(provider, e164, **kw):
    return PhoneNumber(
        organization_id=ORG,
        e164=e164,
        country_code="US",
        number_type="local",
        provider=provider,
        provisioning_state="active",
        **kw,
    )


@pytest.fixture
async def pbx_number(async_session, conn):
    row = _number("pbx", "+14155550100", sip_connection_id=conn.id)
    async_session.add(row)
    await async_session.flush()
    return row


@pytest.fixture
async def twilio_number(async_session):
    row = _number("twilio", "+14155550101")
    async_session.add(row)
    await async_session.flush()
    return row


async def test_connection_of_returns_live_connection(async_session, conn, pbx_number):
    assert (await connection_of(async_session, pbx_number)).id == conn.id


async def test_connection_of_refuses_deleted_connection(
    async_session, conn, pbx_number
):
    conn.deleted_at = T0
    await async_session.flush()
    with pytest.raises(ValueError):
        await connection_of(async_session, pbx_number)


async def test_connection_of_refuses_carrier_number(async_session, twilio_number):
    with pytest.raises(ValueError):
        await connection_of(async_session, twilio_number)


@pytest.mark.parametrize(
    "raw,region,out",
    [
        ("101", "US", "101"),
        ("123456", "US", "123456"),
        ("4155550100", "US", "+14155550100"),
        ("+14155550100", "GB", "+14155550100"),
        ("02079460000", "GB", "+442079460000"),
        ("5550199", "US", "5550199"),
        ("anonymous", "US", "anonymous"),
        ("", "US", ""),
    ],
)
def test_normalize_caller_keeps_extension(raw, region, out):
    assert normalize_caller(raw, region) == out


def test_carrier_caller_matches_old_rule():
    assert carrier_caller("442079460000") == "+442079460000"
    assert carrier_caller("anonymous") == "anonymous"
    assert carrier_caller(" 442079460000 ") == "+442079460000"


def test_only_first_six_digits_count():
    stored = hash_check_code("482913")
    assert check_code_matches("482913", stored) is True
    assert check_code_matches("1482913", stored) is False  # 7 digits: no window search
    assert check_code_matches("48291", stored) is False
    assert check_code_matches("", stored) is False


def test_check_code_hash_is_keyed():
    """A plain sha256 of 6 digits is reversed by trying 1,000,000 codes; the
    stored value is an HMAC keyed from HAIL_PROVIDER_SECRET_KEY."""
    assert CHECK_CODE_DIGITS == 6
    stored = hash_check_code("482913")
    assert stored != hashlib.sha256(b"482913").hexdigest()
    assert "482913" not in stored
    assert check_code_matches("482913", stored) is True
    assert check_code_matches("482914", stored) is False


def test_check_code_hash_changes_with_the_secret_key(monkeypatch):
    stored = hash_check_code("482913")
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())
    assert hash_check_code("482913") != stored
    assert check_code_matches("482913", stored) is False


def test_check_code_without_secret_key_fails_closed(monkeypatch):
    stored = hash_check_code("482913")
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    with pytest.raises(SecretKeyMissing):
        hash_check_code("482913")
    assert check_code_matches("482913", stored) is False


@pytest.mark.parametrize("stored", [None, "", 482913, "é" * 64])
def test_check_code_matches_refuses_a_bad_stored_hash(stored):
    assert check_code_matches("482913", stored) is False


async def _check_call(
    session,
    number,
    status,
    requested_at,
    number_id=None,
    from_pool=True,
    to_e164=None,
    org=ORG,
    caller_e164="+14155550160",
):
    caller = PhoneNumber(
        organization_id=None if from_pool else org,
        is_pool=from_pool,
        e164=caller_e164,
        country_code="US",
        number_type="local",
        provider="twilio",
        provisioning_state="active",
    )
    session.add(caller)
    await session.flush()
    terminal = status in ("completed", "failed", "busy", "no_answer", "canceled")
    call = Call(
        organization_id=org,
        from_number_id=caller.id,
        from_e164=caller.e164,
        to_e164=to_e164 or number.e164,
        direction="outbound",
        status=status,
        end_reason="user_unavailable" if terminal else None,
        voice_config={},
        requested_at=requested_at,
        metadata_={CHECK_CALL_META: str(number_id or number.id)},
    )
    session.add(call)
    await session.flush()
    return call


async def test_check_in_flight_is_the_newest_live_check(async_session, pbx_number):
    await _check_call(async_session, pbx_number, "ringing", T0 - timedelta(seconds=30))
    newest = await _check_call(
        async_session,
        pbx_number,
        "in_progress",
        T0 - timedelta(seconds=5),
        caller_e164="+14155550161",
    )
    assert (await check_in_flight(async_session, pbx_number.id, T0)).id == newest.id


async def test_check_in_flight_skips_finished_checks(async_session, pbx_number):
    await _check_call(async_session, pbx_number, "no_answer", T0)
    await _check_call(
        async_session, pbx_number, "completed", T0, caller_e164="+14155550161"
    )
    assert await check_in_flight(async_session, pbx_number.id, T0) is None


async def test_check_in_flight_skips_checks_older_than_the_window(
    async_session, pbx_number
):
    assert CHECK_WINDOW == timedelta(seconds=90)
    await _check_call(async_session, pbx_number, "ringing", T0 - timedelta(seconds=91))
    assert await check_in_flight(async_session, pbx_number.id, T0) is None
    inside = await _check_call(
        async_session,
        pbx_number,
        "ringing",
        T0 - timedelta(seconds=89),
        caller_e164="+14155550161",
    )
    assert (await check_in_flight(async_session, pbx_number.id, T0)).id == inside.id


async def test_check_in_flight_is_per_number(async_session, pbx_number):
    await _check_call(async_session, pbx_number, "ringing", T0, number_id=uuid.uuid4())
    assert await check_in_flight(async_session, pbx_number.id, T0) is None


async def test_check_in_flight_ignores_a_customer_call_with_forged_metadata(
    async_session, pbx_number
):
    """A call the customer placed from its own number is never a check call,
    whatever its metadata says."""
    await _check_call(async_session, pbx_number, "ringing", T0, from_pool=False)
    assert await check_in_flight(async_session, pbx_number.id, T0) is None


async def test_check_in_flight_ignores_a_call_to_another_number(
    async_session, pbx_number
):
    await _check_call(async_session, pbx_number, "ringing", T0, to_e164="+14155550142")
    assert await check_in_flight(async_session, pbx_number.id, T0) is None


async def test_check_in_flight_ignores_another_orgs_call(async_session, pbx_number):
    await _check_call(async_session, pbx_number, "ringing", T0, org=uuid.uuid4())
    assert await check_in_flight(async_session, pbx_number.id, T0) is None


async def test_check_in_flight_ignores_an_inbound_call(async_session, pbx_number):
    async_session.add(
        Call(
            organization_id=ORG,
            to_number_id=pbx_number.id,
            from_e164="+14155550142",
            to_e164=pbx_number.e164,
            direction="inbound",
            status="ringing",
            voice_config={},
            requested_at=T0,
            metadata_={CHECK_CALL_META: str(pbx_number.id)},
        )
    )
    await async_session.flush()
    assert await check_in_flight(async_session, pbx_number.id, T0) is None


def test_check_keys_are_reserved_on_post_calls():
    assert CHECK_CALL_META.startswith(PBX_CHECK_METADATA_PREFIX)
    assert CHECK_CODE_META.startswith(PBX_CHECK_METADATA_PREFIX)


async def test_check_outcomes_are_this_calls_pbx_check_events(async_session):
    org = uuid.uuid4()
    pool = PhoneNumber(
        organization_id=None,
        e164="+14155550199",
        country_code="US",
        number_type="local",
        capabilities=["voice"],
        provider="twilio",
        provider_resource_id="PN_pool",
        provisioning_state="active",
        is_pool=True,
    )
    async_session.add(pool)
    await async_session.flush()
    calls = []
    for _ in range(2):
        call = Call(
            organization_id=org,
            from_number_id=pool.id,
            from_e164=pool.e164,
            to_e164="+14155550100",
            direction="outbound",
            status="ringing",
            voice_config={},
        )
        async_session.add(call)
        calls.append(call)
    await async_session.flush()
    this, other = calls
    async_session.add_all(
        [
            CallEvent(
                call_id=this.id, kind=CHECK_EVENT_KIND, payload={"outcome": "arrived"}
            ),
            CallEvent(
                call_id=this.id, kind=CHECK_EVENT_KIND, payload={"outcome": "passed"}
            ),
            CallEvent(call_id=this.id, kind="state_change", payload={"outcome": "x"}),
            CallEvent(
                call_id=other.id,
                kind=CHECK_EVENT_KIND,
                payload={"outcome": "not_authenticated"},
            ),
        ]
    )
    await async_session.commit()
    assert await check_outcomes(async_session, this.id) == {"arrived", "passed"}
    assert await check_outcomes(async_session, other.id) == {"not_authenticated"}


def test_arabic_indic_digits_are_not_an_extension():
    # str.isdigit() is true for these; only ASCII digits count.
    assert normalize_caller("١٠١", "US") == "١٠١"
    assert normalize_caller("٤١٥٥٥٥٠١٠٠", "US") == "٤١٥٥٥٥٠١٠٠"
    assert carrier_caller("١٠١") == "١٠١"
