import json
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from hailhq.core.browser_voice import finish_browser_call
from hailhq.core.hmac_signing import sign
from hailhq.core.models import AccountCredit, Agent, Call
from sqlalchemy import select

SECRET = "browser-test-secret"


async def post(client, monkeypatch, path, body):
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_internal_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_api_key", "test-key")
    monkeypatch.setattr(
        settings, "livekit_api_secret", "test-secret-long-enough-for-hmac-testing"
    )
    monkeypatch.setattr(settings, "livekit_url", "wss://test.livekit.cloud")
    raw = json.dumps(body).encode()
    return await client.post(
        "/internal/browser-sessions" + path,
        content=raw,
        headers={
            "content-type": "application/json",
            "x-hail-signature": sign(raw, SECRET),
        },
    )


async def setup_agent(db):
    org = uuid.uuid4()
    agent = Agent(
        organization_id=org,
        name="Reception",
        system_prompt="Collect a callback request.",
        voice_config={},
        tools=[],
    )
    db.add(agent)
    await db.commit()
    return {
        "organization_id": str(org),
        "agent_id": str(agent.id),
        "request_id": str(uuid.uuid4()),
        "rate_cents_per_minute": "10",
    }


@pytest.mark.asyncio
async def test_free_trial_reserves_one_minute_and_token_is_microphone_only(
    client, async_session, livekit_mock, monkeypatch
):
    body = await setup_agent(async_session)
    response = await post(client, monkeypatch, "", body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["trial"] and data["seconds"] == 300
    grants = jwt.decode(data["token"], options={"verify_signature": False})["video"]
    assert grants["canPublishSources"] == ["microphone"]
    assert grants["canPublishData"] is False
    assert grants["room"] == data["room"]
    second = await post(
        client, monkeypatch, "", {**body, "request_id": str(uuid.uuid4())}
    )
    assert second.status_code == 409
    assert (await async_session.execute(select(AccountCredit))).scalars().all() == []


@pytest.mark.asyncio
async def test_wrong_org_and_unsigned_requests_are_rejected(
    client, async_session, monkeypatch
):
    body = await setup_agent(async_session)
    assert (await client.post("/internal/browser-sessions", json=body)).status_code in (
        401,
        503,
    )
    response = await post(
        client, monkeypatch, "", {**body, "organization_id": str(uuid.uuid4())}
    )
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_paid_reservation_refunds_unused_time_once(
    client, async_session, monkeypatch
):
    body = await setup_agent(async_session)
    org = uuid.UUID(body["organization_id"])
    async_session.add(
        AccountCredit(
            organization_id=org,
            kind="credit",
            channel="credit",
            amount_cents=10,
            source="stripe",
            ref="payment",
        )
    )
    await async_session.commit()
    response = await post(client, monkeypatch, "", body)
    assert response.status_code == 200, response.text
    data = response.json()
    assert not data["trial"] and data["seconds"] == 60
    call = await async_session.get(Call, uuid.UUID(data["id"]))
    call.answered_at = datetime.now(timezone.utc) - timedelta(seconds=30)
    await async_session.commit()
    await finish_browser_call(async_session, call.id)
    await async_session.commit()
    await finish_browser_call(async_session, call.id)
    await async_session.commit()
    rows = (
        (
            await async_session.execute(
                select(AccountCredit).where(
                    AccountCredit.source == "browser_voice_refund"
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1 and 4.8 <= float(rows[0].amount_cents) <= 5


@pytest.mark.asyncio
async def test_setup_failure_returns_trial_budget(
    client, async_session, livekit_mock, monkeypatch
):
    body = await setup_agent(async_session)
    livekit_mock.dispatch_agent.side_effect = RuntimeError("no worker")
    response = await post(client, monkeypatch, "", body)
    assert response.status_code == 503
    livekit_mock.dispatch_agent.side_effect = None
    response = await post(
        client, monkeypatch, "", {**body, "request_id": str(uuid.uuid4())}
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_depleted_paying_org_gets_no_new_free_trial(
    client, async_session, monkeypatch
):
    body = await setup_agent(async_session)
    org = uuid.UUID(body["organization_id"])
    async_session.add_all(
        [
            AccountCredit(
                organization_id=org,
                kind="credit",
                channel="credit",
                amount_cents=10,
                source="stripe",
                ref="payment",
            ),
            AccountCredit(
                organization_id=org,
                kind="debit",
                channel="voice",
                amount_cents=-10,
                source="usage_event",
                ref="spent",
            ),
        ]
    )
    await async_session.commit()
    assert (await post(client, monkeypatch, "", body)).status_code == 402


@pytest.mark.asyncio
async def test_simultaneous_tabs_cannot_claim_two_free_minutes(
    client, async_session, monkeypatch, session_factory
):
    import asyncio

    body = await setup_agent(async_session)
    from hailhq.api.main import app
    from hailhq.core.db import get_session

    async def independent_session():
        async with session_factory() as db:
            yield db

    previous = app.dependency_overrides[get_session]
    app.dependency_overrides[get_session] = independent_session
    results = await asyncio.gather(
        post(client, monkeypatch, "", body),
        post(client, monkeypatch, "", {**body, "request_id": str(uuid.uuid4())}),
    )
    app.dependency_overrides[get_session] = previous
    assert sorted(r.status_code for r in results) == [200, 409]


@pytest.mark.asyncio
async def test_trial_only_start_never_turns_into_a_paid_charge(
    client, async_session, monkeypatch
):
    body = await setup_agent(async_session)
    async_session.add(
        AccountCredit(
            organization_id=uuid.UUID(body["organization_id"]),
            kind="credit",
            channel="credit",
            amount_cents=20,
            source="stripe",
            ref="payment",
        )
    )
    await async_session.commit()
    response = await post(client, monkeypatch, "", {**body, "trial_only": True})
    assert response.status_code == 409
    debits = (
        (
            await async_session.execute(
                select(AccountCredit).where(AccountCredit.kind == "debit")
            )
        )
        .scalars()
        .all()
    )
    assert debits == []


@pytest.mark.asyncio
async def test_finalization_refreshes_stale_identity_map_and_uses_media_end_time(
    async_session, session_factory
):
    from decimal import Decimal

    from hailhq.core.browser_voice import session_charge_cents

    org = uuid.uuid4()
    started = datetime.now(timezone.utc) - timedelta(seconds=30)
    call = Call(
        organization_id=org,
        transport="browser",
        direction="inbound",
        provider="browser",
        voice_config={},
        max_duration_seconds=60,
        answered_at=started,
        metadata_={
            "browser_trial": False,
            "browser_rate_cents": "10",
            "browser_reserved_cents": "10",
        },
        status="in_progress",
    )
    async_session.add(call)
    await async_session.commit()
    ended = started + timedelta(seconds=10)
    async with session_factory() as other:
        await finish_browser_call(other, call.id, ended_at=ended)
        await other.commit()
    # This session still holds the non-terminal identity loaded above.
    await finish_browser_call(
        async_session, call.id, ended_at=datetime.now(timezone.utc)
    )
    await async_session.commit()
    await async_session.refresh(call)
    assert call.metadata_["browser_used_ms"] == 10000
    refunds = (
        (
            await async_session.execute(
                select(AccountCredit).where(AccountCredit.organization_id == org)
            )
        )
        .scalars()
        .all()
    )
    assert len(refunds) == 1
    assert refunds[0].amount_cents == Decimal(10) - session_charge_cents(
        10000, "10", 60
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("already_deleted", [False, True])
async def test_stop_is_idempotent_and_releases_unstarted_reservations(
    client, async_session, livekit_mock, monkeypatch, already_deleted
):
    from livekit import api

    body = await setup_agent(async_session)
    started = await post(client, monkeypatch, "", body)
    assert started.status_code == 200
    if already_deleted:
        livekit_mock.delete_room.side_effect = api.TwirpError(
            "not_found", "room gone", status=404
        )
    control = {"organization_id": body["organization_id"], "id": started.json()["id"]}
    stopped = await post(client, monkeypatch, "/stop", control)
    assert stopped.status_code == 200, stopped.text
    assert stopped.json()["status"] == "completed"
    assert stopped.json()["used_ms"] == 0
    assert (await post(client, monkeypatch, "/stop", control)).status_code == 200
    next_call = await post(
        client, monkeypatch, "", {**body, "request_id": str(uuid.uuid4())}
    )
    assert next_call.status_code == 200
    assert next_call.json()["seconds"] == 300


@pytest.mark.asyncio
async def test_worker_crash_releases_paid_hold_without_charging_unknown_duration(
    client, async_session, monkeypatch
):
    from hailhq.core.reconcile import sweep_stale_calls

    body = await setup_agent(async_session)
    org = uuid.UUID(body["organization_id"])
    async_session.add(
        AccountCredit(
            organization_id=org,
            kind="credit",
            channel="credit",
            amount_cents=10,
            source="stripe",
            ref="payment",
        )
    )
    await async_session.commit()
    result = await post(client, monkeypatch, "", body)
    call = await async_session.get(Call, uuid.UUID(result.json()["id"]))
    call.started_at = call.answered_at = datetime.now(timezone.utc) - timedelta(
        seconds=90
    )
    call.status = "in_progress"
    await async_session.commit()
    await sweep_stale_calls(async_session, grace_seconds=0)
    await async_session.commit()
    refund = (
        await async_session.execute(
            select(AccountCredit).where(AccountCredit.source == "browser_voice_refund")
        )
    ).scalar_one()
    assert refund.amount_cents == 10


@pytest.mark.asyncio
async def test_budget_is_org_scoped_and_separates_modality_limits(
    client, async_session, monkeypatch
):
    body = await setup_agent(async_session)
    async_session.add(
        Call(
            id=uuid.uuid4(),
            organization_id=body["organization_id"],
            transport="browser",
            voice_config={},
            livekit_room="budget-history",
            direction="outbound",
            status="completed",
            end_reason="normal_hangup",
            max_duration_seconds=60,
            metadata_={
                "browser_trial": True,
                "browser_used_ms": 15000,
                "browser_actions": [
                    {"channel": "sms"},
                    {"channel": "email"},
                    {"channel": "email"},
                ],
            },
        )
    )
    await async_session.commit()
    response = await post(
        client, monkeypatch, "/budget", {"organization_id": body["organization_id"]}
    )
    assert response.status_code == 200
    assert response.json() == {
        "voice": {"used": 15000, "limit": 300000},
        "sms": {"used": 1, "limit": 3},
        "email": {"used": 2, "limit": 3},
    }
    other = await post(
        client, monkeypatch, "/budget", {"organization_id": str(uuid.uuid4())}
    )
    assert other.json()["voice"]["used"] == 0
    assert other.json()["sms"]["used"] == 0


@pytest.mark.asyncio
async def test_message_preview_limits_persist_across_trial_sessions(async_session):
    from hailhq.core.browser_voice import reserve_browser_preview

    body = await setup_agent(async_session)
    prior = Call(
        id=uuid.uuid4(),
        organization_id=body["organization_id"],
        transport="browser",
        voice_config={},
        livekit_room="prior-previews",
        direction="outbound",
        status="completed",
        end_reason="normal_hangup",
        max_duration_seconds=60,
        metadata_={
            "browser_trial": True,
            "browser_used_ms": 10000,
            "browser_actions": [{"channel": "sms"}] * 3,
        },
    )
    current = Call(
        id=uuid.uuid4(),
        organization_id=body["organization_id"],
        transport="browser",
        voice_config={},
        livekit_room="current-previews",
        direction="outbound",
        status="in_progress",
        max_duration_seconds=50,
        metadata_={"browser_trial": True},
    )
    async_session.add_all([prior, current])
    await async_session.commit()
    assert not await reserve_browser_preview(
        async_session, current.id, {"channel": "sms", "body": "blocked"}
    )
    assert await reserve_browser_preview(
        async_session, current.id, {"channel": "email", "body": "allowed"}
    )
    await async_session.commit()
    assert current.metadata_["browser_actions"] == [
        {"channel": "email", "body": "allowed"}
    ]

    assert await reserve_browser_preview(
        async_session, current.id, {"channel": "email", "body": "second"}
    )
    assert await reserve_browser_preview(
        async_session, current.id, {"channel": "email", "body": "third"}
    )
    assert not await reserve_browser_preview(
        async_session, current.id, {"channel": "email", "body": "over limit"}
    )
    await async_session.commit()
