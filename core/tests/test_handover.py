import uuid

import pytest
from hailhq.core.compliance_gate import add_suppression
from hailhq.core.handover import (
    HandoverInvalid,
    HandoverItem,
    country_of,
    handover_targets,
    load_handover,
    replace_handover,
    validate_handover,
)
from hailhq.core.models import Agent, Contact


async def _agent_and_contacts(s, *phones):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Desk", system_prompt="Help.")
    s.add(agent)
    contacts = []
    for i, phone in enumerate(phones):
        c = Contact(
            organization_id=org,
            name=f"Person {i}",
            phone_e164=phone,
            email=None if phone else f"p{i}@example.com",
        )
        s.add(c)
        contacts.append(c)
    await s.flush()
    return org, agent, contacts


def test_country_of() -> None:
    assert country_of("+14155550100") == "US"
    assert country_of("+447700900123") == "GB"
    assert country_of("+999") is None


async def test_validate_ok(async_session) -> None:
    org, _, (c,) = await _agent_and_contacts(async_session, "+14155550101")
    await validate_handover(async_session, org, [HandoverItem(c.id, "Billing")])


@pytest.mark.parametrize("case", ["other_org", "no_phone", "duplicate", "too_many"])
async def test_validate_rejects(async_session, case) -> None:
    org, _, (c, nophone) = await _agent_and_contacts(
        async_session, "+14155550102", None
    )
    items = {
        "other_org": [HandoverItem(uuid.uuid4(), "x")],
        "no_phone": [HandoverItem(nophone.id, "x")],
        "duplicate": [HandoverItem(c.id, "x"), HandoverItem(c.id, "y")],
        "too_many": [HandoverItem(c.id, "x")] * 11,
    }[case]
    with pytest.raises(HandoverInvalid):
        await validate_handover(async_session, org, items)


async def test_validate_rejects_suppressed(async_session) -> None:
    org, _, (c,) = await _agent_and_contacts(async_session, "+14155550103")
    await add_suppression(
        async_session,
        organization_id=org,
        recipient="+14155550103",
        channel="voice",
        reason="manual",
        source="test",
    )
    with pytest.raises(HandoverInvalid) as err:
        await validate_handover(async_session, org, [HandoverItem(c.id, "x")])
    assert err.value.index == 0


async def test_validate_rejects_unsold_country(async_session) -> None:
    # +882 is an international network code: no carrier catalog lists it.
    org, _, (c,) = await _agent_and_contacts(async_session, "+88213000000")
    with pytest.raises(HandoverInvalid):
        await validate_handover(async_session, org, [HandoverItem(c.id, "x")])


async def test_replace_and_load_keep_order(async_session) -> None:
    _, agent, (a, b) = await _agent_and_contacts(
        async_session, "+14155550104", "+14155550105"
    )
    await replace_handover(
        async_session,
        agent.id,
        [HandoverItem(b.id, "second"), HandoverItem(a.id, "first")],
    )
    rows = (await load_handover(async_session, [agent.id]))[agent.id]
    assert [r["note"] for r in rows] == ["second", "first"]
    assert rows[0]["phone_e164"] == "+14155550105"
    await replace_handover(async_session, agent.id, [])
    assert (await load_handover(async_session, [agent.id])).get(agent.id, []) == []


async def test_targets_have_no_numbers(async_session) -> None:
    _, agent, (a,) = await _agent_and_contacts(async_session, "+14155550106")
    await replace_handover(async_session, agent.id, [HandoverItem(a.id, "Billing")])
    targets = await handover_targets(async_session, agent.id)
    assert targets == [
        {"contact_id": str(a.id), "label": "Person 0", "note": "Billing"}
    ]
    assert "+1415" not in repr(targets)


async def test_targets_skip_contacts_without_phone(async_session) -> None:
    _, agent, (a,) = await _agent_and_contacts(async_session, "+14155550107")
    await replace_handover(async_session, agent.id, [HandoverItem(a.id, "x")])
    a.phone_e164 = None
    a.email = "a@example.com"
    await async_session.flush()
    assert await handover_targets(async_session, agent.id) == []


async def test_targets_dedupe_labels(async_session) -> None:
    _, agent, (a, b) = await _agent_and_contacts(
        async_session, "+14155550108", "+14155550109"
    )
    b.name = a.name
    await replace_handover(
        async_session, agent.id, [HandoverItem(a.id, "x"), HandoverItem(b.id, "y")]
    )
    labels = [t["label"] for t in await handover_targets(async_session, agent.id)]
    assert labels == ["Person 0", "Person 0 (2)"]


async def test_targets_none_agent(async_session) -> None:
    assert await handover_targets(async_session, None) == []
