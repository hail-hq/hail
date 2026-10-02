"""POST /internal/agent/reply-sms: the text agent's reply goes out from the
number the person wrote to, gated like every other send."""

from __future__ import annotations

import json
import uuid

import pytest
from hailhq.core import hmac_signing
from hailhq.core.config import settings
from hailhq.core.models import AccountCredit, Agent, PhoneNumber, Sms
from hailhq.core.sms_ingest import ingest_inbound_sms
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

HMAC_SECRET = "test-internal-secret"
ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


def _signed(body: bytes) -> dict[str, str]:
    return {
        "Content-Type": "application/json",
        "X-Hail-Signature": hmac_signing.sign(body, HMAC_SECRET),
    }


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", HMAC_SECRET)


async def _seed(session: AsyncSession, *, credits=10_000):
    org = uuid.uuid4()
    if credits:
        session.add(
            AccountCredit(
                organization_id=org,
                kind="credit",
                channel="credit",
                amount_cents=credits,
                source="test",
                ref="test",
            )
        )
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Help.")
    session.add(agent)
    await session.flush()
    number = PhoneNumber(
        organization_id=org,
        e164=ORG_NUMBER,
        country_code="US",
        number_type="local",
        provisioning_state="active",
        provider_resource_id="PN",
        sms_agent_id=agent.id,
    )
    session.add(number)
    await session.commit()
    result = await ingest_inbound_sms(
        session,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        body="Can I book?",
        provider_message_sid="SM-in-1",
        opt_out_type=None,
    )
    await session.commit()
    return org, agent, number, result.sms_id


def _payload(sms_id, body="Yes, Tuesday at 10 works.") -> bytes:
    return json.dumps({"sms_id": str(sms_id), "body": body}).encode()


async def test_reply_goes_out_from_the_dialed_number(
    client, async_session, sms_mock
) -> None:
    _org, agent, _number, sms_id = await _seed(async_session)
    body = _payload(sms_id)
    resp = await client.post(
        "/internal/agent/reply-sms", content=body, headers=_signed(body)
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data == {
        "ok": True,
        "state": "done",
        "reason": None,
        "reply_id": data["reply_id"],
    }

    sms_mock.send_sms.assert_awaited_once()
    kwargs = sms_mock.send_sms.await_args.kwargs
    assert kwargs["from_e164"] == ORG_NUMBER and kwargs["to_e164"] == PERSON

    reply = await async_session.get(Sms, uuid.UUID(data["reply_id"]))
    assert reply.agent_id == agent.id
    assert reply.direction == "outbound"
    assert reply.metadata_["reply_to_sms_id"] == str(sms_id)

    # Same inbound id again: no second send.
    resp = await client.post(
        "/internal/agent/reply-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["reply_id"] == data["reply_id"]
    assert sms_mock.send_sms.await_count == 1


async def test_reply_skipped_without_funds(client, async_session, sms_mock) -> None:
    _org, _agent, _number, sms_id = await _seed(async_session, credits=0)
    body = _payload(sms_id)
    resp = await client.post(
        "/internal/agent/reply-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["state"] == "skipped"
    assert resp.json()["reason"] == "insufficient_funds"
    sms_mock.send_sms.assert_not_awaited()


async def test_reply_skipped_after_thread_cap(
    client, async_session, sms_mock, monkeypatch
) -> None:
    from hailhq.api.routes.internal import agent as route_mod

    monkeypatch.setattr(route_mod, "MAX_REPLIES_PER_THREAD", 1)
    org, agent, number, sms_id = await _seed(async_session)
    async_session.add(
        Sms(
            organization_id=org,
            from_number_id=number.id,
            agent_id=agent.id,
            from_e164=ORG_NUMBER,
            to_e164=PERSON,
            direction="outbound",
            status="sent",
            body="earlier reply",
        )
    )
    await async_session.commit()
    body = _payload(sms_id)
    resp = await client.post(
        "/internal/agent/reply-sms", content=body, headers=_signed(body)
    )
    assert resp.json() == {
        "ok": False,
        "state": "skipped",
        "reason": "thread_cap",
        "reply_id": None,
    }
    sms_mock.send_sms.assert_not_awaited()


async def test_reply_unknown_sms_fails(client, async_session) -> None:
    body = _payload(uuid.uuid4())
    resp = await client.post(
        "/internal/agent/reply-sms", content=body, headers=_signed(body)
    )
    assert resp.json()["state"] == "failed"
    assert (await async_session.execute(select(Sms))).first() is None
