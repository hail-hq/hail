"""Agents own the brain settings; numbers and calls point at them."""

from __future__ import annotations

import uuid

from hailhq.core.call_end_reasons import CallEndReason
from hailhq.core.models import (
    Agent,
    Call,
    OrganizationCallSettings,
    PhoneNumber,
    Sms,
)
from sqlalchemy import delete, select


def test_agent_columns():
    cols = {c.name for c in Agent.__table__.columns}
    assert {
        "id",
        "organization_id",
        "name",
        "system_prompt",
        "first_message",
        "ai_disclosure",
        "ai_disclosure_line",
        "voice_config",
        "tools",
        "max_duration_seconds",
        "sms_enabled",
        "status",
        "created_at",
        "updated_at",
    } <= cols


def test_routing_and_inbound_columns():
    pn = {c.name for c in PhoneNumber.__table__.columns}
    assert {"voice_agent_id", "sms_agent_id", "inbound_registered_at"} <= pn
    calls = Call.__table__.c
    assert calls.from_number_id.nullable is True
    assert calls.to_number_id.nullable is True
    assert calls.agent_id.nullable is True
    assert {"calls_number_for_direction"} <= {
        c.name for c in Call.__table__.constraints
    }
    sms = {c.name for c in Sms.__table__.columns}
    assert {"agent_id", "agent_reply_state"} <= sms
    ocs = OrganizationCallSettings.__table__.c
    assert ocs.max_duration_seconds.nullable is True
    assert "ai_disclosure_line" in {
        c.name for c in OrganizationCallSettings.__table__.columns
    }


def test_new_end_reasons():
    assert CallEndReason.INSUFFICIENT_FUNDS.value == "insufficient_funds"
    assert CallEndReason.NO_AGENT.value == "no_agent"


async def test_deleting_an_agent_detaches_its_numbers(async_session) -> None:
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Be kind.")
    async_session.add(agent)
    await async_session.flush()
    pn = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provisioning_state="active",
        voice_agent_id=agent.id,
        sms_agent_id=agent.id,
    )
    async_session.add(pn)
    await async_session.commit()
    pn_id, agent_id = pn.id, agent.id

    await async_session.execute(delete(Agent).where(Agent.id == agent_id))
    await async_session.commit()
    async_session.expire_all()

    row = (
        await async_session.execute(select(PhoneNumber).where(PhoneNumber.id == pn_id))
    ).scalar_one()
    assert row.voice_agent_id is None
    assert row.sms_agent_id is None
