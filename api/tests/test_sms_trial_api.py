"""Routes for US trial texting: status, phone proof start and confirm."""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from hailhq.api import deps
from hailhq.core.config import settings
from hailhq.core.models import AuditLog, Sms, SmsTrialPhone, UsageEvent
from hailhq.core.providers.sms import ProviderSmsResult
from sqlalchemy import func, select, update

from .conftest import (
    insert_org_and_key,
    seed_sms_trial,
    seed_trial_texts,
    verify_trial_phone,
)

TRIAL_E164 = "+18005550199"
PHONE = "+14155550142"
START = {"phone": PHONE, "recipient_consent": True, "consent_version": "2026-11-15"}


def _principal(org_id, user_id, *, console_session: bool = True) -> deps.Principal:
    return deps.Principal(
        auth_kind="jwt",
        api_key_id=None,
        user_id=user_id,
        organization_id=org_id,
        scopes=["*"],
        console_session=console_session,
    )


@pytest.fixture()
async def jwt_client(client, org_and_key) -> AsyncIterator[httpx.AsyncClient]:
    """``client`` as a signed-in console session of ``org_and_key``'s org."""
    from hailhq.api.main import app

    org_id, api_key, _ = org_and_key
    principal = _principal(org_id, uuid.UUID(api_key.reference_id))
    app.dependency_overrides[deps.get_current_principal] = lambda: principal
    try:
        yield client
    finally:
        app.dependency_overrides.pop(deps.get_current_principal, None)


def _code(sms_mock) -> str:
    return re.search(r"code is (\d{6})", sms_mock.send_sms.call_args.kwargs["body"])[1]


def _msg(resp) -> str:
    return resp.json()["detail"][0]["msg"]


async def _row(session_factory, org_id) -> SmsTrialPhone | None:
    async with session_factory() as s:
        return (
            await s.execute(
                select(SmsTrialPhone).where(SmsTrialPhone.organization_id == org_id)
            )
        ).scalar_one_or_none()


async def _audit(session_factory, org_id, action) -> list[AuditLog]:
    async with session_factory() as s:
        return list(
            (
                await s.execute(
                    select(AuditLog).where(
                        AuditLog.organization_id == org_id, AuditLog.action == action
                    )
                )
            ).scalars()
        )


def _result(error_code=None, status="queued"):
    async def _send(**_kwargs):
        return ProviderSmsResult(
            provider_message_sid=None if error_code else "SM_x",
            status=status,
            segment_count=1,
            error_code=error_code,
        )

    return _send


async def test_get_trial_off(client, org_and_key, monkeypatch):
    monkeypatch.setattr(settings, "hail_sms_trial_number", "")
    _, _, plain = org_and_key
    resp = await client.get(
        "/v1/sms/trial", headers={"Authorization": f"Bearer {plain}"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "configured": False,
        "available": False,
        "status": "none",
        "phone": None,
        "code_expires_at": None,
        "texts_used": 0,
        "texts_limit": 25,
        "texts_left": 25,
    }


async def test_get_trial_counts_sent_trial_texts(
    jwt_client, org_and_key, async_session, monkeypatch
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    await seed_trial_texts(async_session, org_id, 3)
    await seed_trial_texts(async_session, org_id, 2, status="failed")

    body = (await jwt_client.get("/v1/sms/trial")).json()
    assert body["texts_used"] == 3 and body["texts_left"] == 22
    assert body["texts_limit"] == 25


async def test_get_trial_configured_not_approved(
    jwt_client, async_session, monkeypatch, sms_mock
):
    number = await seed_sms_trial(async_session, monkeypatch)
    await async_session.execute(
        update(type(number))
        .where(type(number).id == number.id)
        .values(sms_registration_id=None)
    )
    await async_session.commit()

    body = (await jwt_client.get("/v1/sms/trial")).json()
    assert body["configured"] is True and body["available"] is False

    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "Trial texting is not available yet."
    sms_mock.send_sms.assert_not_called()


async def test_get_trial_approved_but_sending_off(
    jwt_client, async_session, monkeypatch, sms_mock
):
    await seed_sms_trial(async_session, monkeypatch)
    monkeypatch.setattr(settings, "hail_sms_trial_sending", False)

    body = (await jwt_client.get("/v1/sms/trial")).json()
    assert body["configured"] is True and body["available"] is False
    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 409, resp.text
    sms_mock.send_sms.assert_not_called()


async def test_get_trial_route_not_captured_by_sms_id(
    jwt_client, async_session, monkeypatch
):
    await seed_sms_trial(async_session, monkeypatch)
    for path in ("/v1/sms/trial", "/sms/trial"):
        resp = await jwt_client.get(path)
        assert resp.status_code == 200, (path, resp.text)
        assert resp.json()["available"] is True


async def test_start_needs_console_session(
    client, org_and_key, async_session, monkeypatch, sms_mock
):
    from hailhq.api.main import app

    await seed_sms_trial(async_session, monkeypatch)
    org_id, api_key, plain = org_and_key
    user_id = uuid.UUID(api_key.reference_id)
    auth = {"Authorization": f"Bearer {plain}"}

    for path, body in (
        ("/v1/sms/trial/phone", START),
        ("/v1/sms/trial/phone/confirm", {"code": "123456"}),
    ):
        resp = await client.post(path, json=body, headers=auth)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "Set up trial texting in the console."

    app.dependency_overrides[deps.get_current_principal] = lambda: _principal(
        org_id, user_id, console_session=False
    )
    try:
        for path, body in (
            ("/v1/sms/trial/phone", START),
            ("/v1/sms/trial/phone/confirm", {"code": "123456"}),
        ):
            resp = await client.post(path, json=body)
            assert resp.status_code == 403, resp.text
    finally:
        app.dependency_overrides.pop(deps.get_current_principal, None)
    sms_mock.send_sms.assert_not_called()


async def test_start_texts_a_code_from_the_trial_number(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)

    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 200, resp.text

    call = sms_mock.send_sms.call_args.kwargs
    assert call["from_e164"] == TRIAL_E164 and call["to_e164"] == PHONE
    assert call["status_callback_url"] is None
    assert re.search(
        r"code is \d{6}\. It expires in 10 minutes\. Reply STOP", call["body"]
    )
    body = resp.json()
    assert body["status"] == "pending" and body["phone"] == PHONE
    assert body["code_expires_at"] is not None
    assert body["configured"] is True and body["available"] is True

    row = await _row(session_factory, org_id)
    assert row is not None and row.status == "pending"
    assert row.consent_version == "2026-11-15"
    assert row.consent_user_id == uuid.UUID(org_and_key[1].reference_id)

    audits = await _audit(session_factory, org_id, "sms.trial.code_sent")
    assert len(audits) == 1 and audits[0].payload == {"phone": PHONE}
    assert _code(sms_mock) not in str(audits[0].payload)

    assert (await jwt_client.get("/v1/sms/trial")).json()["status"] == "pending"


async def test_code_text_is_not_an_sms_row_and_not_billed(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    await seed_sms_trial(async_session, monkeypatch)
    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 200, resp.text

    async with session_factory() as s:
        assert (await s.execute(select(func.count()).select_from(Sms))).scalar() == 0
        usage = (await s.execute(select(func.count()).select_from(UsageEvent))).scalar()
        assert usage == 0
    assert resp.json()["texts_used"] == 0 and resp.json()["texts_left"] == 25


@pytest.mark.parametrize(
    "case", ["non_us", "too_short", "hail_number", "taken", "sixth_code"]
)
async def test_start_maps_errors(
    jwt_client, async_session, monkeypatch, sms_mock, case
):
    await seed_sms_trial(async_session, monkeypatch)
    body = dict(START)
    if case == "non_us":
        body["phone"] = "+447700900123"
    elif case == "too_short":
        body["phone"] = "+12345"
    elif case == "hail_number":
        body["phone"] = TRIAL_E164
    elif case == "taken":
        other_org, _, _ = await insert_org_and_key(
            async_session, org_name="Other", org_slug="other"
        )
        await verify_trial_phone(async_session, other_org, PHONE)
    else:
        for _ in range(5):
            ok = await jwt_client.post("/v1/sms/trial/phone", json=START)
            assert ok.status_code == 200, ok.text
    sends = sms_mock.send_sms.call_count

    resp = await jwt_client.post("/v1/sms/trial/phone", json=body)

    if case in ("non_us", "too_short"):
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["loc"] == ["body", "phone"]
        assert _msg(resp) == "Trial texts go to US mobile numbers only."
    elif case == "hail_number":
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"][0]["loc"] == ["body", "phone"]
        assert _msg(resp) == "Use your own mobile phone, not a Hail number."
    elif case == "taken":
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"].startswith("Another workspace uses this phone")
    else:
        assert resp.status_code == 429, resp.text
        assert int(resp.headers["Retry-After"]) > 0
        assert resp.json()["detail"] == (
            "This phone got the maximum of 5 codes in the last 24 hours. "
            "Try again later."
        )
    assert sms_mock.send_sms.call_count == sends


async def test_start_reports_stop_refusal(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    sms_mock.send_sms.side_effect = _result(error_code="21610")

    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "phone"]
    assert _msg(resp) == "This phone replied STOP to Hail. Reply START, then try again."
    assert await _row(session_factory, org_id) is None
    assert await _audit(session_factory, org_id, "sms.trial.code_sent") == []


@pytest.mark.parametrize(
    "send,status_code,detail",
    [
        (
            _result(error_code="21610"),
            422,
            "This phone replied STOP to Hail. Reply START, then try again.",
        ),
        (
            _result(error_code="30006"),
            422,
            "Hail could not text this phone. Check the number.",
        ),
        (
            _result(status="failed"),
            422,
            "Hail could not text this phone. Check the number.",
        ),
        (None, 502, "Hail could not send the code. Try again."),
    ],
    ids=["stop", "rejected", "failed_status", "exception"],
)
async def test_failed_send_uses_no_budget_and_changes_no_row(
    jwt_client,
    org_and_key,
    async_session,
    monkeypatch,
    sms_mock,
    session_factory,
    send,
    status_code,
    detail,
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    assert (await jwt_client.post("/v1/sms/trial/phone", json=START)).status_code == 200
    before = await _row(session_factory, org_id)

    if send is None:

        async def send(**_kwargs):
            raise RuntimeError("connection reset")

    sms_mock.send_sms.side_effect = send
    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == status_code, resp.text
    got = _msg(resp) if status_code == 422 else resp.json()["detail"]
    assert got == detail

    after = await _row(session_factory, org_id)
    assert after.code_hash == before.code_hash
    assert after.code_expires_at == before.code_expires_at
    assert len(after.code_send_log) == 1
    assert len(await _audit(session_factory, org_id, "sms.trial.code_sent")) == 1

    # A later good send logs one more entry: the failed one left nothing in
    # the session either.
    sms_mock.send_sms.side_effect = _result()
    assert (await jwt_client.post("/v1/sms/trial/phone", json=START)).status_code == 200
    assert len((await _row(session_factory, org_id)).code_send_log) == 2


async def test_start_again_after_verified_sends_nothing(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id, PHONE)

    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "verified" and resp.json()["phone"] == PHONE
    sms_mock.send_sms.assert_not_called()
    assert await _audit(session_factory, org_id, "sms.trial.code_sent") == []


async def test_confirm_flow(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)

    resp = await jwt_client.post("/v1/sms/trial/phone/confirm", json={"code": "123456"})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "code"]
    assert _msg(resp) == "Send a code first."

    assert (await jwt_client.post("/v1/sms/trial/phone", json=START)).status_code == 200
    code = _code(sms_mock)
    wrong = f"{(int(code) + 1) % 10**6:06d}"

    resp = await jwt_client.post("/v1/sms/trial/phone/confirm", json={"code": wrong})
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["loc"] == ["body", "code"]
    assert _msg(resp) == "The code is not correct. 4 tries left."

    resp = await jwt_client.post("/v1/sms/trial/phone/confirm", json={"code": code})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "verified" and body["phone"] == PHONE
    assert body["code_expires_at"] is None

    row = await _row(session_factory, org_id)
    assert row.status == "verified" and row.code_hash is None
    audits = await _audit(session_factory, org_id, "sms.trial.verified")
    assert len(audits) == 1 and audits[0].payload == {"phone": PHONE}
    assert (await jwt_client.get("/v1/sms/trial")).json()["status"] == "verified"


async def test_confirm_expired_code(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    assert (await jwt_client.post("/v1/sms/trial/phone", json=START)).status_code == 200
    async with session_factory() as s:
        await s.execute(
            update(SmsTrialPhone)
            .where(SmsTrialPhone.organization_id == org_id)
            .values(code_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
        )
        await s.commit()
    async_session.expire_all()

    resp = await jwt_client.post(
        "/v1/sms/trial/phone/confirm", json={"code": _code(sms_mock)}
    )
    assert resp.status_code == 422, resp.text
    assert _msg(resp) == "The code expired. Send a new code."


async def test_wrong_code_try_is_committed(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    """A wrong try survives the request: a new session reads the count, so
    a rollback cannot hand out extra tries."""
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    assert (await jwt_client.post("/v1/sms/trial/phone", json=START)).status_code == 200
    wrong = f"{(int(_code(sms_mock)) + 1) % 10**6:06d}"

    resp = await jwt_client.post("/v1/sms/trial/phone/confirm", json={"code": wrong})
    assert resp.status_code == 422, resp.text
    await async_session.rollback()
    assert (await _row(session_factory, org_id)).code_attempts == 1

    for _ in range(4):
        resp = await jwt_client.post(
            "/v1/sms/trial/phone/confirm", json={"code": wrong}
        )
        assert resp.status_code == 422, resp.text
    assert _msg(resp) == "The code is not correct. Send a new code."
    await async_session.rollback()
    row = await _row(session_factory, org_id)
    assert row.code_attempts == 5 and row.code_hash is None

    resp = await jwt_client.post(
        "/v1/sms/trial/phone/confirm", json={"code": _code(sms_mock)}
    )
    assert resp.status_code == 422, resp.text
    assert _msg(resp) == "Send a code first."


async def test_confirm_phone_taken_meanwhile(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    org_id = org_and_key[0]
    await seed_sms_trial(async_session, monkeypatch)
    assert (await jwt_client.post("/v1/sms/trial/phone", json=START)).status_code == 200
    other_org, _, _ = await insert_org_and_key(
        async_session, org_name="Other", org_slug="other"
    )
    await verify_trial_phone(async_session, other_org, PHONE)

    resp = await jwt_client.post(
        "/v1/sms/trial/phone/confirm", json={"code": _code(sms_mock)}
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"].startswith("Another workspace uses this phone")
    assert (await _row(session_factory, org_id)).status == "pending"
    # The session is still usable after the refused verify.
    resp = await jwt_client.get("/v1/sms/trial")
    assert resp.status_code == 200 and resp.json()["status"] == "pending"


async def test_errors_never_name_a_carrier(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock
):
    texts = []
    number = await seed_sms_trial(async_session, monkeypatch)
    number_id = number.id

    async def post(path, body):
        texts.append((await jwt_client.post(path, json=body)).text)

    await post("/v1/sms/trial/phone/confirm", {"code": "123456"})
    for phone in ("+447700900123", TRIAL_E164):
        await post("/v1/sms/trial/phone", {**START, "phone": phone})
    for send in (_result(error_code="21610"), _result(error_code="30006")):
        sms_mock.send_sms.side_effect = send
        await post("/v1/sms/trial/phone", START)

    async def boom(**_kwargs):
        raise RuntimeError("Twilio connection reset")

    sms_mock.send_sms.side_effect = boom
    await post("/v1/sms/trial/phone", START)
    sms_mock.send_sms.side_effect = _result()
    await post("/v1/sms/trial/phone", START)
    await post("/v1/sms/trial/phone/confirm", {"code": "000000"})

    await async_session.execute(
        update(type(number))
        .where(type(number).id == number_id)
        .values(sms_registration_id=None)
    )
    await async_session.commit()
    await post("/v1/sms/trial/phone", START)

    assert len(texts) == 9
    for text in texts:
        assert "twilio" not in text.lower() and "telnyx" not in text.lower(), text


async def test_code_goes_to_the_stored_phone(
    jwt_client, org_and_key, async_session, monkeypatch, sms_mock, session_factory
):
    """The route texts and audits the phone core stored, not the request's."""
    from hailhq.api.routes import sms_trial as route
    from hailhq.core.sms_trial import start_phone_proof

    org_id = org_and_key[0]
    stored = "+14155550143"
    await seed_sms_trial(async_session, monkeypatch)

    async def _stores_other(db, organization_id, **kwargs):
        return await start_phone_proof(
            db, organization_id, **{**kwargs, "phone": stored}
        )

    monkeypatch.setattr(route, "start_phone_proof", _stores_other)
    resp = await jwt_client.post("/v1/sms/trial/phone", json=START)
    assert resp.status_code == 200, resp.text
    assert sms_mock.send_sms.call_args.kwargs["to_e164"] == stored
    audits = await _audit(session_factory, org_id, "sms.trial.code_sent")
    assert audits[0].payload == {"phone": stored}


def test_confirm_409_is_documented_as_phone_taken():
    from hailhq.api.main import app

    spec = app.openapi()
    confirm = spec["paths"]["/v1/sms/trial/phone/confirm"]["post"]["responses"]
    assert confirm["409"]["description"] == (
        "Another workspace uses this phone for trial texts. Use a different phone."
    )
