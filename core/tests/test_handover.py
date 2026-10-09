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
    assert country_of("+447400123456") == "GB"
    assert country_of("+447700900123") is None  # reserved range: invalid
    assert country_of("+999") is None


async def test_validate_ok(async_session) -> None:
    org, _, (c,) = await _agent_and_contacts(async_session, "+14155550101")
    await validate_handover(async_session, org, [HandoverItem(str(c.id), "Billing")])


@pytest.mark.parametrize("case", ["other_org", "no_phone", "duplicate", "too_many"])
async def test_validate_rejects(async_session, case) -> None:
    org, _, (c, nophone) = await _agent_and_contacts(
        async_session, "+14155550102", None
    )
    items = {
        "other_org": [HandoverItem(str(uuid.uuid4()), "x")],
        "no_phone": [HandoverItem(str(nophone.id), "x")],
        "duplicate": [HandoverItem(str(c.id), "x"), HandoverItem(str(c.id), "y")],
        "too_many": [HandoverItem(str(uuid.uuid4()), "x") for _ in range(11)],
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
        await validate_handover(async_session, org, [HandoverItem(str(c.id), "x")])
    assert err.value.index == 0


async def test_validate_rejects_unsold_country(async_session) -> None:
    # +882 is an international network code: no carrier catalog lists it.
    org, _, (c,) = await _agent_and_contacts(async_session, "+88213000000")
    with pytest.raises(HandoverInvalid):
        await validate_handover(async_session, org, [HandoverItem(str(c.id), "x")])


async def test_replace_and_load_keep_order(async_session) -> None:
    _, agent, (a, b) = await _agent_and_contacts(
        async_session, "+14155550104", "+14155550105"
    )
    await replace_handover(
        async_session,
        agent.id,
        [HandoverItem(str(b.id), "second"), HandoverItem(str(a.id), "first")],
    )
    rows = (await load_handover(async_session, [agent.id]))[agent.id]
    assert [r["note"] for r in rows] == ["second", "first"]
    assert rows[0]["phone_e164"] == "+14155550105"
    await replace_handover(async_session, agent.id, [])
    assert (await load_handover(async_session, [agent.id])).get(agent.id, []) == []


async def test_targets_have_no_numbers(async_session) -> None:
    _, agent, (a,) = await _agent_and_contacts(async_session, "+14155550106")
    await replace_handover(
        async_session, agent.id, [HandoverItem(str(a.id), "Billing")]
    )
    targets = await handover_targets(async_session, agent.id)
    assert targets == [
        {"contact_id": str(a.id), "label": "Person 0", "note": "Billing"}
    ]
    assert "+1415" not in repr(targets)


async def test_targets_skip_contacts_without_phone(async_session) -> None:
    _, agent, (a,) = await _agent_and_contacts(async_session, "+14155550107")
    await replace_handover(async_session, agent.id, [HandoverItem(str(a.id), "x")])
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
        async_session,
        agent.id,
        [HandoverItem(str(a.id), "x"), HandoverItem(str(b.id), "y")],
    )
    labels = [t["label"] for t in await handover_targets(async_session, agent.id)]
    assert labels == ["Person 0", "Person 0 (2)"]


async def test_targets_none_agent(async_session) -> None:
    assert await handover_targets(async_session, None) == []


# --- team members (wire id ``member:<user uuid>``) -------------------------


async def _member(s, org, name, phone):
    from datetime import datetime, timezone

    from hailhq.core.models import OrganizationMember, User

    user = User(
        id=uuid.uuid4(),
        name=name,
        email=f"{uuid.uuid4().hex}@example.com",
        phone_number=phone,
        created_at=datetime.now(timezone.utc),
    )
    s.add(user)
    s.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=user.id,
            organization_id=org,
            role="member",
            created_at=datetime.now(timezone.utc),
        )
    )
    await s.flush()
    return user


def test_handover_wire_ids() -> None:
    from hailhq.core.contact_ids import normalize_contact_id, parse_contact_id

    u = uuid.uuid4()
    assert parse_contact_id(str(u)) == ("contact", u)
    assert parse_contact_id(f"member:{u}") == ("member", u)
    assert normalize_contact_id(str(u).upper()) == str(u)
    assert normalize_contact_id(f"member:{str(u).upper()}") == f"member:{u}"
    for bad in ("", "member:", "member:nope", "nope", f"user:{u}"):
        with pytest.raises(ValueError):
            parse_contact_id(bad)


async def test_validate_member_ok(async_session) -> None:
    org, _, _ = await _agent_and_contacts(async_session)
    m = await _member(async_session, org, "Sam", "+14155550120")
    await validate_handover(async_session, org, [HandoverItem(f"member:{m.id}", "x")])


@pytest.mark.parametrize("case", ["other_org", "no_phone", "duplicate", "unknown"])
async def test_validate_member_rejects(async_session, case) -> None:
    org, _, _ = await _agent_and_contacts(async_session)
    other = await _member(async_session, uuid.uuid4(), "Other", "+14155550121")
    nophone = await _member(async_session, org, "Nophone", None)
    m = await _member(async_session, org, "Sam", "+14155550122")
    items = {
        "other_org": [HandoverItem(f"member:{other.id}", "x")],
        "no_phone": [HandoverItem(f"member:{nophone.id}", "x")],
        "duplicate": [
            HandoverItem(f"member:{m.id}", "x"),
            HandoverItem(f"member:{m.id}", "y"),
        ],
        "unknown": [HandoverItem(f"member:{uuid.uuid4()}", "x")],
    }[case]
    with pytest.raises(HandoverInvalid):
        await validate_handover(async_session, org, items)


async def test_validate_member_rejects_suppressed(async_session) -> None:
    org, _, (c,) = await _agent_and_contacts(async_session, "+14155550123")
    m = await _member(async_session, org, "Sam", "+14155550124")
    await add_suppression(
        async_session,
        organization_id=org,
        recipient="+14155550124",
        channel="voice",
        reason="manual",
        source="test",
    )
    with pytest.raises(HandoverInvalid) as err:
        await validate_handover(
            async_session,
            org,
            [HandoverItem(str(c.id), "x"), HandoverItem(f"member:{m.id}", "y")],
        )
    assert err.value.index == 1


async def test_validate_member_rejects_unsold_country(async_session) -> None:
    org, _, _ = await _agent_and_contacts(async_session)
    m = await _member(async_session, org, "Sam", "+88213000001")
    with pytest.raises(HandoverInvalid):
        await validate_handover(
            async_session, org, [HandoverItem(f"member:{m.id}", "x")]
        )


async def test_validate_unchanged_member_skips_checks(async_session) -> None:
    org, _, _ = await _agent_and_contacts(async_session)
    m = await _member(async_session, org, "Sam", "+88213000002")
    wire = f"member:{m.id}"
    await validate_handover(
        async_session, org, [HandoverItem(wire, "x")], frozenset({wire})
    )


async def test_replace_and_load_mixed(async_session) -> None:
    org, agent, (c,) = await _agent_and_contacts(async_session, "+14155550125")
    m = await _member(async_session, org, "Sam", "+14155550126")
    await replace_handover(
        async_session,
        agent.id,
        [HandoverItem(f"member:{m.id}", "first"), HandoverItem(str(c.id), "second")],
    )
    rows = (await load_handover(async_session, [agent.id]))[agent.id]
    assert rows == [
        {
            "contact_id": f"member:{m.id}",
            "name": "Sam",
            "phone_e164": "+14155550126",
            "note": "first",
        },
        {
            "contact_id": str(c.id),
            "name": "Person 0",
            "phone_e164": "+14155550125",
            "note": "second",
        },
    ]


async def test_load_skips_member_removed_from_org(async_session) -> None:
    from hailhq.core.models import OrganizationMember
    from sqlalchemy import delete

    org, agent, _ = await _agent_and_contacts(async_session)
    m = await _member(async_session, org, "Sam", "+14155550127")
    await replace_handover(
        async_session, agent.id, [HandoverItem(f"member:{m.id}", "x")]
    )
    await async_session.execute(
        delete(OrganizationMember).where(OrganizationMember.user_id == m.id)
    )
    assert (await load_handover(async_session, [agent.id])).get(agent.id, []) == []
    assert await handover_targets(async_session, agent.id) == []


async def test_targets_include_members_and_dedupe_across_kinds(async_session) -> None:
    org, agent, (c,) = await _agent_and_contacts(async_session, "+14155550128")
    m = await _member(async_session, org, "Person 0", "+14155550129")
    await replace_handover(
        async_session,
        agent.id,
        [HandoverItem(str(c.id), "x"), HandoverItem(f"member:{m.id}", "y")],
    )
    targets = await handover_targets(async_session, agent.id)
    assert targets == [
        {"contact_id": str(c.id), "label": "Person 0", "note": "x"},
        {"contact_id": f"member:{m.id}", "label": "Person 0 (2)", "note": "y"},
    ]
    assert "+1415" not in repr(targets)


async def test_targets_contacts_only_without_member_tables(async_session) -> None:
    """Self-host: no website-owned users/members tables. Targets fall back to
    contacts, and the session stays usable (no aborted transaction)."""
    from sqlalchemy import text

    _, agent, (c,) = await _agent_and_contacts(async_session, "+14155550130")
    await replace_handover(async_session, agent.id, [HandoverItem(str(c.id), "x")])
    await async_session.execute(text("DROP TABLE users CASCADE"))
    await async_session.execute(text("DROP TABLE members CASCADE"))
    targets = await handover_targets(async_session, agent.id)
    assert [t["contact_id"] for t in targets] == [str(c.id)]
    assert (await async_session.execute(text("SELECT 1"))).scalar() == 1


async def test_member_lookup_reraises_other_schema_errors(async_session) -> None:
    """Only a missing table means self-host. A renamed column (or lost
    privilege) is a real fault and must not silently drop members."""
    from sqlalchemy import text
    from sqlalchemy.exc import ProgrammingError

    org, agent, _ = await _agent_and_contacts(async_session)
    m = await _member(async_session, org, "Sam", "+14155550131")
    await replace_handover(
        async_session, agent.id, [HandoverItem(f"member:{m.id}", "x")]
    )
    await async_session.execute(
        text("ALTER TABLE users RENAME COLUMN phone_number TO phone_x")
    )
    with pytest.raises(ProgrammingError):
        await handover_targets(async_session, agent.id)
    with pytest.raises(ProgrammingError):
        await validate_handover(
            async_session, org, [HandoverItem(f"member:{m.id}", "x")]
        )


async def test_missing_member_tables_warn_once(async_session, monkeypatch, caplog):
    import logging

    from hailhq.core import handover
    from sqlalchemy import text

    monkeypatch.setattr(handover, "_warned_no_member_tables", False)
    from hailhq.core.contact_ids import contact_wire_id

    _, agent, _ = await _agent_and_contacts(async_session)
    m = await _member(async_session, agent.organization_id, "Sam", "+14155550132")
    await replace_handover(
        async_session,
        agent.id,
        [HandoverItem(contact_wire_id("member", m.id), "x")],
    )
    await async_session.execute(text("DROP TABLE members CASCADE"))
    with caplog.at_level(logging.DEBUG, logger="hailhq.core.handover"):
        await handover_targets(async_session, agent.id)
        await handover_targets(async_session, agent.id)
    levels = [r.levelno for r in caplog.records if r.name == "hailhq.core.handover"]
    assert levels == [logging.WARNING, logging.DEBUG]
