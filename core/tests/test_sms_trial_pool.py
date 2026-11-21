"""Operator changes to trial texting numbers (scripts/ops/sms_trial_numbers.py)."""

import os
import uuid
from pathlib import Path

import pytest
from hailhq.core import sms_trial_pool
from hailhq.core.models import Agent, CarrierVerification, Organization, PhoneNumber
from hailhq.core.pool import claim_pool_number
from hailhq.core.sms_trial import APPROVED_AT_KEY, trial_configured, trial_sender
from hailhq.core.sms_trial_pool import TrialPoolError

E164 = "+18005550199"


async def _org(session):
    org = uuid.uuid4()
    session.add(Organization(id=org, origin="human"))
    await session.flush()
    return org


async def _number(session, *, org=None, number_type="toll_free", **kw):
    number = PhoneNumber(
        organization_id=org,
        is_pool=org is None,
        e164=kw.pop("e164", E164),
        country_code=kw.pop("country_code", "US"),
        number_type=number_type,
        capabilities=kw.pop("capabilities", ["voice", "sms"]),
        provider="twilio",
        provider_resource_id="PN0199",
        provisioning_state=kw.pop("state", "active"),
        **kw,
    )
    session.add(number)
    await session.flush()
    return number


async def test_mark_dry_run_changes_nothing(async_session):
    number = await _number(async_session)
    lines = await sms_trial_pool.mark(async_session, E164, apply=False)
    assert "leaves the call pool" in lines[0]
    assert number.pool_purpose is None


async def test_mark_takes_a_call_pool_number_out_of_the_call_pool(async_session):
    number = await _number(async_session)
    await sms_trial_pool.mark(async_session, E164, apply=True)
    assert number.pool_purpose == "sms_trial"
    assert await trial_configured(async_session)
    assert await claim_pool_number(async_session) is None


@pytest.mark.parametrize(
    "case,message",
    [
        ("owned", "Use move"),
        ("local", "not a US toll-free number"),
        ("not_us", "not a US toll-free number"),
        ("voice_only", "cannot text"),
        ("marked", "already a trial texting number"),
        ("missing", "no live number"),
    ],
)
async def test_mark_refuses(async_session, case, message):
    kw = {}
    if case == "owned":
        kw["org"] = await _org(async_session)
    elif case == "local":
        kw["number_type"] = "local"
    elif case == "not_us":
        kw["country_code"] = "CA"
    elif case == "voice_only":
        kw["capabilities"] = ["voice"]
    elif case == "marked":
        kw["pool_purpose"] = "sms_trial"
    if case != "missing":
        await _number(async_session, **kw)
    with pytest.raises(TrialPoolError, match=message):
        await sms_trial_pool.mark(async_session, E164, apply=True)


async def test_unmark_returns_the_number_to_the_call_pool(async_session):
    number = await _number(
        async_session,
        pool_purpose="sms_trial",
        provisioning_metadata={APPROVED_AT_KEY: "2026-11-20T00:00:00+00:00"},
    )
    assert await trial_sender(async_session) is not None
    await sms_trial_pool.unmark(async_session, E164, apply=True)
    assert number.pool_purpose is None
    assert APPROVED_AT_KEY not in number.provisioning_metadata
    assert await trial_sender(async_session) is None
    assert (await claim_pool_number(async_session)).id == number.id


async def test_unmark_refuses_a_number_that_cannot_call(async_session):
    await _number(async_session, pool_purpose="sms_trial", capabilities=["sms"])
    with pytest.raises(TrialPoolError, match="cannot make calls"):
        await sms_trial_pool.unmark(async_session, E164, apply=True)


async def test_unmark_refuses_a_call_pool_number(async_session):
    await _number(async_session)
    with pytest.raises(TrialPoolError, match="not a trial texting number"):
        await sms_trial_pool.unmark(async_session, E164, apply=True)


async def test_move_puts_a_workspace_number_in_the_trial_pool(async_session):
    org = await _org(async_session)
    number = await _number(async_session, org=org)
    registration = CarrierVerification(
        id=uuid.uuid4(),
        organization_id=org,
        number_id=number.id,
        provider="twilio",
        country_code="US",
        number_type="toll_free",
        subject_type="business",
        purpose="sms",
        state="approved",
        requirements_version="test",
    )
    async_session.add(registration)
    await async_session.flush()
    number.sms_registration_id = registration.id
    number.messaging_service_sid = "MG0001"
    await async_session.flush()

    dry = await sms_trial_pool.move(async_session, E164, apply=False)
    assert number.organization_id == org
    assert "Do not cancel that registration" in dry[1]

    await sms_trial_pool.move(async_session, E164, apply=True)
    await async_session.flush()
    assert number.organization_id is None and number.is_pool
    assert number.pool_purpose == "sms_trial"
    assert number.sms_registration_id is None
    # Kept: the number still texts through its messaging service.
    assert number.messaging_service_sid == "MG0001"
    assert await trial_configured(async_session)
    assert await claim_pool_number(async_session) is None


async def test_move_refuses_a_number_an_agent_answers(async_session):
    org = await _org(async_session)
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Be brief.")
    async_session.add(agent)
    await async_session.flush()
    await _number(async_session, org=org, sms_agent_id=agent.id)
    with pytest.raises(TrialPoolError, match="an agent answers it"):
        await sms_trial_pool.move(async_session, E164, apply=True)


async def test_move_refuses_a_pool_number(async_session):
    await _number(async_session)
    with pytest.raises(TrialPoolError, match="Use mark"):
        await sms_trial_pool.move(async_session, E164, apply=True)


async def test_status_names_the_sender(async_session):
    await _number(
        async_session,
        pool_purpose="sms_trial",
        provisioning_metadata={APPROVED_AT_KEY: "2026-11-20T00:00:00+00:00"},
    )
    await _number(async_session, e164="+18005550198", pool_purpose="sms_trial")
    lines = await sms_trial_pool.status(async_session)
    assert any(line.startswith(E164) and "sends now" in line for line in lines)
    assert any(
        line.startswith("+18005550198") and "not ready" in line for line in lines
    )


def test_no_trial_env_vars_left():
    """Trial numbers are rows in the database. No code, .env.example key or
    doc names the removed variables (old plans and the CHANGELOG excepted:
    they record history)."""
    root = Path(__file__).resolve().parents[2]
    names = ("hail_sms_trial_number", "hail_sms_trial_sending")
    pruned = {".git", ".venv", "node_modules", ".claude", "superpowers", ".next"}
    suffixes = {".py", ".md", ".example", ".yaml", ".yml", ".ts", ".tsx", ".go"}
    found = []
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in pruned]
        for name in files:
            path = Path(folder, name)
            if name in {"CHANGELOG.md", Path(__file__).name}:
                continue
            if path.suffix not in suffixes:
                continue
            text = path.read_text(errors="ignore").lower()
            found += [f"{path.relative_to(root)}: {n}" for n in names if n in text]
    assert found == []
