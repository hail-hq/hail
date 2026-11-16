"""Trial texts from POST /v1/sms: a workspace with no approved US
registration sends to its verified trial phone from Hail's trial number."""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.config import settings
from hailhq.core.models import (
    AgentSendLog,
    CarrierVerification,
    Organization,
    PhoneNumber,
    Sms,
    UsageEvent,
)
from hailhq.core.sms_registration_gate import BLOCK_MESSAGE
from hailhq.core.sms_trial import (
    SMS_TRIAL_META_KEY,
    TRIAL_HINT,
    TrialBodyTooLong,
    trial_texts_used,
)
from sqlalchemy import event, func, select, text, update

from .conftest import (
    approve_us_sms,
    insert_org_and_key,
    seed_sms_trial,
    seed_trial_texts,
    verify_trial_phone,
)
from .test_sms_api import STATUS_AUTH_TOKEN, _signed_status_form

TRIAL_E164 = "+18005550199"
PHONE = "+14155550142"
OTHER_PHONE = "+14155550143"
OWN_NUMBER = "+14155550144"
CA_NUMBER = "+14165550145"
OWN_TOLLFREE = "+18005550111"
WRAPPED_HI = "[Hail trial] hi\nHail does not read replies."


def _auth(plaintext: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {plaintext}"}


def _payload(to: str = PHONE, **extra) -> dict:
    return {"to": to, "body": "hi", "recipient_consent": True, **extra}


def _msg(resp) -> str:
    return resp.json()["detail"][0]["msg"]


async def _sms_count(session, org_id) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Sms).where(Sms.organization_id == org_id)
        )
    ).scalar_one()


async def _own_number(session, org_id, e164: str = OWN_NUMBER) -> None:
    session.add(
        PhoneNumber(
            organization_id=org_id,
            e164=e164,
            country_code="US",
            number_type="local",
            capabilities=["sms"],
            provider="twilio",
            provider_resource_id=f"PN_{e164}",
            provisioning_state="active",
        )
    )
    await session.commit()


async def _own_trial_number(session, org_id) -> None:
    """Move the trial number and its registration to ``org_id``: that
    workspace becomes the trial number's owner."""
    number_id = (
        await session.execute(
            select(PhoneNumber.id).where(PhoneNumber.e164 == TRIAL_E164)
        )
    ).scalar_one()
    await session.execute(
        update(PhoneNumber)
        .where(PhoneNumber.id == number_id)
        .values(organization_id=org_id)
    )
    await session.execute(
        update(CarrierVerification)
        .where(CarrierVerification.number_id == number_id)
        .values(organization_id=org_id)
    )
    await session.commit()


async def _holds_advisory_lock(session) -> bool:
    """True when this session's connection holds an advisory lock, such as
    the workspace lock a trial claim takes."""
    return (
        await session.execute(
            text(
                "SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                "AND pid = pg_backend_pid()"
            )
        )
    ).scalar_one() > 0


def _trial_sends(sms_mock) -> list:
    return [
        c
        for c in sms_mock.send_sms.call_args_list
        if c.kwargs["from_e164"] == TRIAL_E164
    ]


@pytest.mark.parametrize(
    "case,hint",
    [
        ("not_verified_phone", True),
        ("pending_phone", True),
        ("other_workspace_phone", True),
        ("cap_used_25", False),
        ("explicit_own_unregistered_from", True),
        ("feature_off", False),
        ("owner_workspace", False),
    ],
)
async def test_trial_send_refused(
    case, hint, client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """Each case is the passing setup with one condition changed."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    payload = _payload()
    if case == "not_verified_phone":
        await verify_trial_phone(async_session, org_id)
        payload = _payload(to=OTHER_PHONE)
    elif case == "pending_phone":
        await verify_trial_phone(async_session, org_id, status="pending")
    elif case == "other_workspace_phone":
        other_id, _, _ = await insert_org_and_key(
            async_session, org_name="Other", org_slug="other"
        )
        await verify_trial_phone(async_session, other_id, PHONE)
        await verify_trial_phone(async_session, org_id, OTHER_PHONE)
    elif case == "cap_used_25":
        await verify_trial_phone(async_session, org_id)
        await seed_trial_texts(async_session, org_id, 25)
    elif case == "explicit_own_unregistered_from":
        await verify_trial_phone(async_session, org_id)
        await _own_number(async_session, org_id)
        payload = _payload(**{"from": OWN_NUMBER})
    elif case == "feature_off":
        await verify_trial_phone(async_session, org_id)
        monkeypatch.setattr(settings, "hail_sms_trial_sending", False)
    elif case == "owner_workspace":
        # Hail's own workspace (the trial number's owner) sends from another
        # of its numbers that is not registered.
        await verify_trial_phone(async_session, org_id)
        await _own_trial_number(async_session, org_id)
        await _own_number(async_session, org_id)
        payload = _payload(**{"from": OWN_NUMBER})
    before = await _sms_count(async_session, org_id)

    resp = await client.post("/sms", json=payload, headers=_auth(plaintext))

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "from"]
    assert _msg(resp).endswith(" " + TRIAL_HINT) is hint
    assert _trial_sends(sms_mock) == []
    assert await _sms_count(async_session, org_id) == before


async def test_explicit_own_from_422_names_the_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)

    resp = await client.post(
        "/sms", json=_payload(**{"from": OWN_NUMBER}), headers=_auth(plaintext)
    )

    assert resp.status_code == 422
    assert _msg(resp) == BLOCK_MESSAGE + " " + TRIAL_HINT
    sms_mock.send_sms.assert_not_awaited()


async def test_trial_send_with_no_number(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms",
        json=_payload(metadata={"run": "a"}),
        headers=_auth(plaintext),
    )

    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["from_e164"] == TRIAL_E164
    assert out["body"] == WRAPPED_HI
    sent = sms_mock.send_sms.call_args.kwargs
    assert sent["body"] == WRAPPED_HI
    assert sent["from_e164"] == TRIAL_E164
    assert sent["to_e164"] == PHONE
    row = await async_session.get(Sms, uuid.UUID(out["id"]))
    assert row.body == WRAPPED_HI
    assert row.from_e164 == TRIAL_E164
    assert row.metadata_ == {"run": "a", SMS_TRIAL_META_KEY: True}
    usage = (
        await async_session.execute(
            select(UsageEvent).where(UsageEvent.channel == "sms")
        )
    ).scalar_one()
    assert usage.organization_id == org_id
    assert usage.ref == f"sms:{out['id']}:us"


async def test_from_equal_to_trial_number_uses_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms", json=_payload(**{"from": TRIAL_E164}), headers=_auth(plaintext)
    )

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == TRIAL_E164
    assert sms_mock.send_sms.call_args.kwargs["body"] == WRAPPED_HI
    assert await trial_texts_used(async_session, org_id) == 1


async def test_own_unregistered_number_omits_from_for_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """The path the hint names: a workspace whose own number is not
    registered omits ``from`` and the text goes out as a trial text."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == TRIAL_E164
    assert sms_mock.send_sms.call_args.kwargs["body"] == WRAPPED_HI


async def test_registered_number_preferred(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)
    await approve_us_sms(async_session, org_id)

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == OWN_NUMBER
    sent = sms_mock.send_sms.call_args.kwargs
    assert sent["from_e164"] == OWN_NUMBER
    assert sent["body"] == "hi"
    assert await trial_texts_used(async_session, org_id) == 0


async def test_own_ca_number_sends_without_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """A Canadian own number passes the US registration gate, so a from-less
    text to the verified trial phone goes out from it, not as a trial."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    async_session.add(
        PhoneNumber(
            organization_id=org_id,
            e164=CA_NUMBER,
            country_code="CA",
            number_type="local",
            capabilities=["sms"],
            provider="twilio",
            provider_resource_id="PN_ca",
            provisioning_state="active",
        )
    )
    await async_session.commit()

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == CA_NUMBER
    sent = sms_mock.send_sms.call_args.kwargs
    assert sent["from_e164"] == CA_NUMBER
    assert sent["body"] == "hi"
    assert _trial_sends(sms_mock) == []
    assert await trial_texts_used(async_session, org_id) == 0


async def test_live_approved_tollfree_sends_without_trial(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """An own toll-free number the carrier reports approved, with no
    registration row, passes the gate: the text goes out from it, and the
    carrier is asked once."""
    from hailhq.core import carrier_routing

    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    async_session.add(
        PhoneNumber(
            organization_id=org_id,
            e164=OWN_TOLLFREE,
            country_code="US",
            number_type="toll_free",
            capabilities=["sms"],
            provider="twilio",
            provider_resource_id="PN_own_tf",
            provisioning_state="active",
        )
    )
    await async_session.commit()
    lookup = AsyncMock(return_value=True)
    monkeypatch.setattr(
        carrier_routing,
        "sms_route",
        lambda provider: SimpleNamespace(tollfree_approved=lookup),
    )

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == OWN_TOLLFREE
    assert sms_mock.send_sms.call_args.kwargs["body"] == "hi"
    assert _trial_sends(sms_mock) == []
    assert await trial_texts_used(async_session, org_id) == 0
    lookup.assert_awaited_once()


async def test_hail_own_workspace_sends_normally(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """The trial number's owner sends from it as its own number, with no
    trial wrapping, even to a verified trial phone of its own."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await _own_trial_number(async_session, org_id)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms", json=_payload(**{"from": TRIAL_E164}), headers=_auth(plaintext)
    )

    assert resp.status_code == 201, resp.text
    out = resp.json()
    assert out["body"] == "hi"
    assert sms_mock.send_sms.call_args.kwargs["body"] == "hi"
    row = await async_session.get(Sms, uuid.UUID(out["id"]))
    assert SMS_TRIAL_META_KEY not in row.metadata_


async def test_trial_body_too_long_is_422(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post(
        "/sms",
        json={**_payload(), "body": "x" * 1600},
        headers=_auth(plaintext),
    )

    assert resp.status_code == 422
    assert resp.json()["detail"][0]["loc"] == ["body", "body"]
    assert _msg(resp) == str(TrialBodyTooLong(1600))
    # The refusal ended the claim's transaction, so it released the
    # workspace lock. (The route shares this session, which could re-take
    # its own lock, so a follow-up send alone would not show it.)
    assert not async_session.in_transaction()
    sms_mock.send_sms.assert_not_awaited()
    assert await _sms_count(async_session, org_id) == 0

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert resp.status_code == 201, resp.text


async def test_metadata_cannot_plant_marker(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await _own_number(async_session, org_id)
    await approve_us_sms(async_session, org_id)

    resp = await client.post(
        "/sms",
        json=_payload(metadata={SMS_TRIAL_META_KEY: True}),
        headers=_auth(plaintext),
    )

    assert resp.status_code == 422
    assert "metadata keys are reserved: sms_trial" in _msg(resp)
    sms_mock.send_sms.assert_not_awaited()
    assert await _sms_count(async_session, org_id) == 0


async def test_26th_trial_text_refused(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """The route's own text carries the marker and counts toward the cap."""
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await seed_trial_texts(async_session, org_id, 24)

    first = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert first.status_code == 201, first.text
    assert await trial_texts_used(async_session, org_id) == 25

    second = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert second.status_code == 422
    # All 25 used: the refusal does not point to the trial.
    assert TRIAL_HINT not in _msg(second)
    # The refused claim took the workspace lock; the route released it
    # before going on.
    assert not await _holds_advisory_lock(async_session)
    assert len(_trial_sends(sms_mock)) == 1
    assert await _sms_count(async_session, org_id) == 25


async def test_cap_counts_only_this_workspace(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    other_id, _, _ = await insert_org_and_key(
        async_session, org_name="Other", org_slug="other"
    )
    await seed_trial_texts(async_session, other_id, 25, to=OTHER_PHONE)
    await verify_trial_phone(async_session, org_id)

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["from_e164"] == TRIAL_E164


async def test_trial_text_refused_at_create_is_recorded_and_not_counted(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    from hailhq.core.providers.sms import ProviderSmsResult

    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    sms_mock.send_sms.side_effect = None
    sms_mock.send_sms.return_value = ProviderSmsResult(
        provider_message_sid="SM_stopped",
        status="failed",
        segment_count=1,
        error_code="21610",
    )

    resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 201, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["error_code"] == "21610"
    assert await trial_texts_used(async_session, org_id) == 0


async def test_trial_text_refused_by_callback_frees_the_cap(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    org_id, _, plaintext = org_and_key
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    monkeypatch.setattr(settings, "twilio_auth_token", STATUS_AUTH_TOKEN)
    monkeypatch.setattr(settings, "hail_api_url", "http://t")

    sent = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert sent.status_code == 201, sent.text
    assert await trial_texts_used(async_session, org_id) == 1
    sid = (
        await async_session.get(Sms, uuid.UUID(sent.json()["id"]))
    ).provider_message_sid

    form, headers = _signed_status_form(
        {"MessageSid": sid, "MessageStatus": "failed", "ErrorCode": "21610"}
    )
    cb = await client.post("/sms/status", data=form, headers=headers)
    assert cb.status_code == 200
    assert cb.json() == {"status": "applied"}

    async_session.expire_all()
    assert await trial_texts_used(async_session, org_id) == 0
    row = await async_session.get(Sms, uuid.UUID(sent.json()["id"]))
    assert row.error_code == "21610"


async def _agent_org_with_ca_number(session, org_id, monkeypatch) -> None:
    """An agent-origin workspace whose only text number is Canadian, with an
    SMS cap of 2 an hour. Its sends to the US pass the US registration gate,
    so they go out from it with no trial claim."""
    session.add(Organization(id=org_id, origin="agent"))
    session.add(
        PhoneNumber(
            organization_id=org_id,
            e164=CA_NUMBER,
            country_code="CA",
            number_type="local",
            capabilities=["sms"],
            provider="twilio",
            provider_resource_id="PN_ca",
            provisioning_state="active",
        )
    )
    await session.commit()
    monkeypatch.setattr(settings, "agent_sms_per_hour", 2)


async def _agent_send_logs(session, org_id) -> int:
    return (
        await session.execute(
            select(func.count())
            .select_from(AgentSendLog)
            .where(AgentSendLog.organization_id == org_id)
        )
    ).scalar_one()


@contextmanager
def _rolled_back_statements(session) -> Iterator[list[list[str]]]:
    """The SQL of each transaction that ended in a rollback while the block
    runs, one list per transaction."""
    engine = session.bind.sync_engine
    open_: dict[int, list[str]] = {}
    rolled_back: list[list[str]] = []

    def on_execute(conn, cursor, statement, parameters, context, executemany):
        open_.setdefault(id(conn), []).append(statement)

    def on_commit(conn):
        open_.pop(id(conn), None)

    def on_rollback(conn):
        rolled_back.append(open_.pop(id(conn), []))

    listeners = [
        ("before_cursor_execute", on_execute),
        ("commit", on_commit),
        ("rollback", on_rollback),
    ]
    for name, fn in listeners:
        event.listen(engine, name, fn)
    try:
        yield rolled_back
    finally:
        for name, fn in listeners:
            event.remove(engine, name, fn)


@pytest.mark.parametrize("trial", ["off", "all_used"])
async def test_refused_trial_claim_keeps_agent_caps(
    trial, client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """The send goes out from the workspace's Canadian number, which passes
    the US gate, so no trial claim runs; with the trial on and used up, the
    sends must still count toward the agent caps."""
    org_id, _, plaintext = org_and_key
    await _agent_org_with_ca_number(async_session, org_id, monkeypatch)
    if trial == "all_used":
        await seed_sms_trial(async_session, monkeypatch)
        await verify_trial_phone(async_session, org_id)
        await seed_trial_texts(async_session, org_id, 25)

    codes = []
    for _ in range(4):
        resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
        codes.append(resp.status_code)

    assert codes == [201, 201, 429, 429]
    assert await _agent_send_logs(async_session, org_id) == 2
    assert [c.kwargs["from_e164"] for c in sms_mock.send_sms.call_args_list] == [
        CA_NUMBER,
        CA_NUMBER,
    ]
    assert not await _holds_advisory_lock(async_session)


async def test_refused_trial_claim_rolls_back_only_reads(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """Everything the route ran before it rolls back a refused claim is a
    read, so the rollback drops no row. The own number is blocked, so the
    claim runs, is refused (all texts used) and the gate answers 422 after
    the agent caps."""
    org_id, _, plaintext = org_and_key
    async_session.add(Organization(id=org_id, origin="agent"))
    await async_session.commit()
    await _own_number(async_session, org_id)
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)
    await seed_trial_texts(async_session, org_id, 25)

    with _rolled_back_statements(async_session) as rolled_back:
        resp = await client.post("/sms", json=_payload(), headers=_auth(plaintext))

    assert resp.status_code == 422, resp.text
    assert _msg(resp) == BLOCK_MESSAGE
    # The claim's lock marks the refused claim's transaction.
    claim_txns = [
        txn for txn in rolled_back if any("pg_advisory_xact_lock" in s for s in txn)
    ]
    assert len(claim_txns) == 1
    statements = claim_txns[0]
    writes = [
        s
        for s in statements
        if s.lstrip().split(None, 1)[0].upper()
        not in {"SELECT", "SAVEPOINT", "RELEASE"}
    ]
    assert writes == []


async def test_trial_send_counts_toward_agent_caps(
    client, async_session, org_and_key, sms_mock, monkeypatch
) -> None:
    """A trial send logs one agent send. Over the cap it is refused: no
    text, no trial text used, and the workspace lock is released."""
    org_id, _, plaintext = org_and_key
    async_session.add(Organization(id=org_id, origin="agent"))
    await async_session.commit()
    monkeypatch.setattr(settings, "agent_sms_per_hour", 1)
    await seed_sms_trial(async_session, monkeypatch)
    await verify_trial_phone(async_session, org_id)

    first = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert first.status_code == 201, first.text
    assert first.json()["from_e164"] == TRIAL_E164
    assert await _agent_send_logs(async_session, org_id) == 1

    second = await client.post("/sms", json=_payload(), headers=_auth(plaintext))
    assert second.status_code == 429
    assert second.json()["detail"] == "sms cap reached: 1/hour per workspace"
    assert second.headers["Retry-After"] == "3600"
    assert not await _holds_advisory_lock(async_session)
    assert len(_trial_sends(sms_mock)) == 1
    assert await trial_texts_used(async_session, org_id) == 1
    assert await _sms_count(async_session, org_id) == 1
    assert await _agent_send_logs(async_session, org_id) == 1
