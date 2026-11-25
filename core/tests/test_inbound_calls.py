"""open_inbound_call: who answers, who is refused, and what gets written."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core import inbound_calls
from hailhq.core.config import settings
from hailhq.core.models import (
    AccountCredit,
    Agent,
    AgentHandoverContact,
    Call,
    CallEvent,
    Contact,
    OrganizationCallSettings,
    PhoneNumber,
    SipConnection,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.secret_cipher import generate_key
from hailhq.core.sip_connections import (
    CHECK_CALL_META,
    CHECK_CODE_META,
    CHECK_EVENT_KIND,
    CHECK_WINDOW,
    hash_check_code,
)
from sqlalchemy import func, select, text

DIALED = "+14155550100"
CALLER = "+33612345678"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
CheckArrived = inbound_calls.CheckArrived
Rejected = inbound_calls.Rejected
finish_check = inbound_calls.finish_check
open_inbound_call = inbound_calls.open_inbound_call


@pytest.fixture(autouse=True)
def trunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_in_tx")
    monkeypatch.setattr(settings, "hail_voice_max_duration_seconds", 300)


@pytest.fixture(autouse=True)
def no_org_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake(_org: str) -> str | None:
        return "Acme Dental"

    monkeypatch.setattr(inbound_calls, "fetch_organization_name", fake)


async def _seed(session, *, credits=10_000, agent=True, org=None, **number_kwargs):
    org = org or uuid.uuid4()
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
    agent_row = None
    if agent:
        agent_row = Agent(
            organization_id=org,
            name="Front desk",
            system_prompt="Book appointments.",
            first_message="How can I help?",
            voice_config={"voice_id": "v1", "language": "fr"},
            tools=["end_call"],
            max_duration_seconds=600,
        )
        session.add(agent_row)
        await session.flush()
    kwargs = {
        "organization_id": org,
        "e164": DIALED,
        "country_code": "US",
        "number_type": "local",
        "provider": "twilio",
        "provisioning_state": "active",
        "voice_agent_id": agent_row.id if agent_row else None,
    }
    kwargs.update(number_kwargs)
    number = PhoneNumber(**kwargs)
    session.add(number)
    session.add(
        WebhookSubscription(
            organization_id=org,
            target_url="https://example.com/hook",
            secret_encrypted="fake",
            status="active",
            event_types=["call.received", "call.failed"],
        )
    )
    await session.commit()
    return org, agent_row, number


def _attrs(**over) -> inbound_calls.SipAttributes:
    base = {
        "dialed": DIALED,
        "caller": CALLER,
        "trunk_id": "ST_in_tw",
        "room_name": "hail-in-abc",
        "provider_call_sid": "sip-call-1",
    }
    base.update(over)
    return inbound_calls.SipAttributes(**base)


async def _deliveries(session, event_type: str) -> list[WebhookDelivery]:
    rows = (
        (
            await session.execute(
                select(WebhookDelivery).where(WebhookDelivery.event_type == event_type)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def test_unknown_number_is_dropped_without_a_row(async_session) -> None:
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "unknown_number"
    assert (await async_session.execute(select(Call))).first() is None


async def test_pool_number_is_dropped(async_session) -> None:
    async_session.add(
        PhoneNumber(
            organization_id=None,
            is_pool=True,
            e164=DIALED,
            country_code="US",
            number_type="local",
            provisioning_state="active",
        )
    )
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "unknown_number"


async def test_wrong_trunk_for_the_carrier_writes_a_failed_call(
    async_session, caplog
) -> None:
    await _seed(async_session)
    with caplog.at_level("WARNING", logger="hailhq.core.inbound_calls"):
        outcome = await inbound_calls.open_inbound_call(
            async_session, _attrs(trunk_id="ST_in_tx")
        )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.id == outcome.call_id
    assert call.status == "failed"
    assert call.end_reason == "carrier_route_failed"
    assert call.direction == "inbound"
    event = (await async_session.execute(select(CallEvent))).scalar_one()
    assert event.payload["reason"] == "carrier_route_failed"
    assert any(r.levelname == "WARNING" for r in caplog.records)


async def test_number_without_voice_writes_a_failed_call(async_session) -> None:
    _org, _agent, number = await _seed(async_session)
    number.capabilities = ["sms"]
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"
    assert (await async_session.execute(select(Call))).scalar_one().status == "failed"


async def test_shared_trunk_serves_every_carrier(async_session, monkeypatch) -> None:
    """One LiveKit inbound trunk for all carriers (what LiveKit allows for a
    wildcard trunk): a DIDWW number on the shared trunk is answered."""
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_shared")
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "ST_shared")
    await _seed(async_session, provider="didww", e164="+351300509184")
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(dialed="+351300509184", trunk_id="ST_shared")
    )
    assert isinstance(outcome, inbound_calls.Accepted)


async def _pbx_seed(
    session, inbound_trunk_id="ST_in_pbx", outbound_trunk_id=None, **number_kwargs
):
    org = uuid.uuid4()
    conn = SipConnection(
        organization_id=org,
        label="Main office",
        inbound_username="hail-aabbccddeeff",
        encrypted_inbound_password="token",
        default_region="US",
        pbx_host="pbx.example.com" if outbound_trunk_id else None,
        livekit_inbound_trunk_id=inbound_trunk_id,
        livekit_outbound_trunk_id=outbound_trunk_id,
    )
    session.add(conn)
    await session.flush()
    return await _seed(
        session,
        provider="pbx",
        sip_connection_id=conn.id,
        org=org,
        capabilities=["voice"],
        **number_kwargs,
    )


async def test_pbx_call_on_its_connection_trunk_is_accepted(async_session) -> None:
    await _pbx_seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_pbx")
    )
    assert isinstance(outcome, inbound_calls.Accepted)


async def test_pbx_call_on_other_trunk_is_refused(async_session) -> None:
    await _pbx_seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_tw")
    )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"


async def test_missing_trunk_id_is_refused(async_session) -> None:
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs(trunk_id=""))
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"


async def test_no_agent_writes_a_failed_call(async_session) -> None:
    _org, _agent, _number = await _seed(async_session, agent=False)
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "no_agent"
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.direction == "inbound"
    assert call.status == "failed"
    assert call.end_reason == "no_agent"
    assert call.to_number_id == _number.id
    assert call.from_number_id is None
    assert call.from_e164 == CALLER and call.to_e164 == DIALED
    assert len(await _deliveries(async_session, "call.failed")) == 1
    assert await _deliveries(async_session, "call.received") == []


async def test_paused_agent_counts_as_no_agent(async_session) -> None:
    _org, agent, _number = await _seed(async_session)
    agent.status = "paused"
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "no_agent"


async def test_no_funds_writes_insufficient_funds(async_session) -> None:
    await _seed(async_session, credits=0)
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "insufficient_funds"
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.status == "failed"
    assert call.end_reason == "insufficient_funds"
    deliveries = await _deliveries(async_session, "call.failed")
    assert deliveries[0].payload["data"]["end_reason"] == "insufficient_funds"
    assert deliveries[0].payload["data"]["direction"] == "inbound"


async def test_accepted_call_rings_and_returns_dispatch_metadata(async_session) -> None:
    org, agent, number = await _seed(async_session)
    async_session.add(
        OrganizationCallSettings(
            organization_id=org,
            max_duration_seconds=120,
            ai_disclosure_line="You reached {org}. I am an AI.",
        )
    )
    await async_session.commit()

    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())

    assert isinstance(outcome, inbound_calls.Accepted)
    call = (await async_session.execute(select(Call))).scalar_one()
    assert call.status == "ringing"
    assert call.direction == "inbound"
    assert call.agent_id == agent.id
    assert call.to_number_id == number.id
    assert call.provider == "twilio"
    assert call.provider_call_sid == "sip-call-1"
    assert call.livekit_room == "hail-in-abc"
    assert call.started_at is not None
    assert call.max_duration_seconds == 600  # the agent's cap wins over the workspace
    assert call.metadata_ == {
        "billed": True,
        "recording_enabled": False,
        "recording_status": "disabled",
        "recording_retention_days": 30,
    }
    assert call.voice_config == {"voice_id": "v1", "language": "fr"}

    events = (await async_session.execute(select(CallEvent))).scalars().all()
    assert [e.payload for e in events] == [{"from": "queued", "to": "ringing"}]
    received = await _deliveries(async_session, "call.received")
    assert len(received) == 1
    assert received[0].payload["data"] == {
        "id": str(call.id),
        "status": "ringing",
        "direction": "inbound",
        "from": CALLER,
        "to": DIALED,
        "agent_id": str(agent.id),
    }

    md = outcome.metadata
    assert md["call_id"] == call.id
    assert md["organization_id"] == str(org)
    assert md["direction"] == "inbound"
    assert md["system_prompt"] == "Book appointments."
    assert md["first_message"] == "How can I help?"
    assert md["ai_disclosure"] is True
    assert md["ai_disclosure_line"] == "You reached {org}. I am an AI."
    assert md["org_name"] == "Acme Dental"
    assert md["tools"] == ["end_call"]
    assert md["handover_targets"] == []
    assert md["max_duration_seconds"] == 600
    assert md["handover_max_duration_seconds"] == 1800  # the default: 30 minutes
    assert md["voice_config"] == {"voice_id": "v1", "language": "fr"}
    assert md["llm"] is None


async def test_agent_line_beats_workspace_line_and_workspace_cap_applies(
    async_session,
) -> None:
    org, agent, _number = await _seed(async_session)
    agent.ai_disclosure_line = "Agent line for {org}."
    agent.max_duration_seconds = None
    async_session.add(
        OrganizationCallSettings(
            organization_id=org, max_duration_seconds=120, ai_disclosure_line="WS."
        )
    )
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Accepted)
    assert outcome.metadata["ai_disclosure_line"] == "Agent line for {org}."
    assert outcome.metadata["max_duration_seconds"] == 120


async def test_service_default_cap_when_nothing_is_set(async_session) -> None:
    _org, agent, _number = await _seed(async_session)
    agent.max_duration_seconds = None
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Accepted)
    assert outcome.metadata["max_duration_seconds"] == 300
    assert outcome.metadata["ai_disclosure_line"] is None


async def test_agent_that_does_not_answer_calls_counts_as_no_agent(
    async_session,
) -> None:
    _org, agent, _number = await _seed(async_session)
    agent.voice_enabled = False
    await async_session.commit()
    outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "no_agent"


async def test_inbound_call_snapshots_recording_retention(async_session):
    org, _, _ = await _seed(async_session)
    workspace = OrganizationCallSettings(
        organization_id=org,
        recording_enabled=True,
        recording_retention_days=30,
    )
    async_session.add(workspace)
    await async_session.commit()
    result = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(result, inbound_calls.Accepted)
    call = (
        await async_session.execute(select(Call).where(Call.organization_id == org))
    ).scalar_one()
    assert call.metadata_["recording_retention_days"] == 30
    workspace.recording_retention_days = 7
    await async_session.commit()
    await async_session.refresh(call)
    assert call.metadata_["recording_retention_days"] == 30


async def test_org_name_comes_from_the_organizations_table(
    async_session, monkeypatch
) -> None:
    async def fail(_org: str) -> str | None:
        raise AssertionError("hail.so must not be asked when the row has a name")

    org = uuid.uuid4()
    await async_session.execute(
        text("INSERT INTO organizations (id, name) VALUES (:id, :name)"),
        {"id": org, "name": "  Acme Dental  "},
    )
    await async_session.commit()
    monkeypatch.setattr(inbound_calls, "fetch_organization_name", fail)
    assert await inbound_calls._organization_name(org) == "Acme Dental"


async def test_org_name_falls_back_to_hail_so_without_a_row(async_session) -> None:
    assert await inbound_calls._organization_name(uuid.uuid4()) == "Acme Dental"


async def test_org_name_falls_back_to_hail_so_when_the_db_fails(monkeypatch) -> None:
    def broken():
        raise OSError("db down")

    monkeypatch.setattr(inbound_calls, "session_scope", broken)
    assert await inbound_calls._organization_name(uuid.uuid4()) == "Acme Dental"


async def test_pbx_call_answers_with_normalized_caller(async_session) -> None:
    await _pbx_seed(async_session)
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(caller="4155550199", trunk_id="ST_in_pbx")
    )
    assert isinstance(out, inbound_calls.Accepted)
    assert out.metadata["from_e164"] == "+14155550199"
    call = await async_session.get(Call, out.metadata["call_id"])
    assert call.from_e164 == "+14155550199"


async def test_pbx_call_from_extension_keeps_digits(async_session) -> None:
    await _pbx_seed(async_session)
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(caller="101", trunk_id="ST_in_pbx")
    )
    assert out.metadata["from_e164"] == "101"


async def test_refused_pbx_call_row_uses_normalized_caller(async_session) -> None:
    await _pbx_seed(async_session)
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(caller="4155550199", trunk_id="ST_in_tw")
    )
    assert isinstance(out, inbound_calls.Rejected)
    call = await async_session.get(Call, out.call_id)
    assert call.from_e164 == "+14155550199"


async def test_carrier_call_caller_still_gets_plus(async_session) -> None:
    await _seed(async_session)
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(caller="14155550199")
    )
    assert out.metadata["from_e164"] == "+14155550199"


async def test_refused_call_of_deleted_connection_keeps_extension(
    async_session,
) -> None:
    _org, _agent, number = await _pbx_seed(async_session)
    conn = await async_session.get(SipConnection, number.sip_connection_id)
    conn.deleted_at = datetime.now(timezone.utc)
    await async_session.commit()
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(caller="101", trunk_id="ST_in_pbx")
    )
    assert isinstance(out, inbound_calls.Rejected)
    call = await async_session.get(Call, out.call_id)
    assert call.from_e164 == "101"


async def _handover_contacts(session, org, agent) -> None:
    """One contact with a phone, one with only an extension."""
    phone = Contact(organization_id=org, name="Sam Lee", phone_e164="+14155550123")
    ext = Contact(organization_id=org, name="Front desk", extension="800")
    session.add_all([phone, ext])
    await session.flush()
    session.add_all(
        [
            AgentHandoverContact(
                agent_id=agent.id, contact_id=phone.id, note="Sales", position=0
            ),
            AgentHandoverContact(
                agent_id=agent.id, contact_id=ext.id, note="Desk", position=1
            ),
        ]
    )
    await session.commit()


async def test_pbx_call_offers_phone_and_extension_targets(async_session) -> None:
    org, agent, _ = await _pbx_seed(async_session, outbound_trunk_id="ST_out_pbx")
    await _handover_contacts(async_session, org, agent)
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_pbx")
    )
    labels = [t["label"] for t in out.metadata["handover_targets"]]
    assert labels == ["Sam Lee", "Front desk"]


async def test_answer_only_pbx_call_offers_no_handover(async_session) -> None:
    """A phone system that only answers has no address for the second leg:
    every handover would fail, so the agent is offered none."""
    org, agent, _ = await _pbx_seed(async_session)
    await _handover_contacts(async_session, org, agent)
    out = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_pbx")
    )
    assert isinstance(out, inbound_calls.Accepted)
    assert out.metadata["handover_targets"] == []


# Ownership check, inbound side.


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def attrs(**over) -> inbound_calls.SipAttributes:
    return _attrs(**over)


@pytest.fixture(autouse=True)
def secret_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check code hash is keyed from HAIL_PROVIDER_SECRET_KEY."""
    monkeypatch.setattr(settings, "hail_provider_secret_key", generate_key())


@pytest.fixture()
async def unverified_number(async_session):
    """``+14155550100`` on a connection whose inbound trunk is ``ST_in_pbx``."""
    _org, _agent, number = await _pbx_seed(
        async_session, provisioning_state="unverified"
    )
    return number


@pytest.fixture()
def seed_check_call(async_session):
    async def seed(
        number,
        status,
        requested_at,
        code="482913",
        number_id=None,
        from_pool=True,
        to_e164=None,
    ):
        pool = PhoneNumber(
            organization_id=None if from_pool else number.organization_id,
            is_pool=from_pool,
            e164="+14155550199",
            country_code="US",
            number_type="local",
            provider="twilio",
            provisioning_state="active",
        )
        async_session.add(pool)
        await async_session.flush()
        terminal = status in ("completed", "failed", "busy", "no_answer", "canceled")
        metadata = {CHECK_CALL_META: str(number_id or number.id)}
        if code is not None:
            metadata[CHECK_CODE_META] = hash_check_code(code)
        call = Call(
            organization_id=number.organization_id,
            from_number_id=pool.id,
            from_e164=pool.e164,
            to_e164=to_e164 or number.e164,
            direction="outbound",
            status=status,
            end_reason="user_unavailable" if terminal else None,
            voice_config={},
            requested_at=requested_at,
            metadata_=metadata,
        )
        async_session.add(call)
        await async_session.commit()
        return call

    return seed


@pytest.fixture()
def last_check_event(async_session):
    async def last(check):
        return (
            await async_session.execute(
                select(CallEvent)
                .where(
                    CallEvent.call_id == check.id, CallEvent.kind == CHECK_EVENT_KIND
                )
                .order_by(CallEvent.occurred_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    return last


async def check_events(session, check) -> list[dict]:
    rows = (
        await session.execute(
            select(CallEvent.payload)
            .where(CallEvent.call_id == check.id, CallEvent.kind == CHECK_EVENT_KIND)
            .order_by(CallEvent.occurred_at)
        )
    ).scalars()
    return list(rows)


async def count_rows(session, model) -> int:
    return await session.scalar(select(func.count()).select_from(model))


async def test_check_arrival_on_connection_trunk_does_not_activate_yet(
    async_session, unverified_number, seed_check_call, last_check_event
):
    check = await seed_check_call(
        unverified_number, status="ringing", requested_at=utcnow()
    )
    out = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx")
    )
    assert out == CheckArrived(unverified_number.id, check.id)
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "arrived"}


async def test_right_code_activates(
    async_session, unverified_number, seed_check_call, last_check_event
):
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    assert (
        await finish_check(
            async_session,
            CheckArrived(unverified_number.id, check.id),
            "482913",
            now=NOW,
        )
        is True
    )
    assert unverified_number.provisioning_state == "active"
    assert unverified_number.acquired_at == NOW
    assert (await last_check_event(check)).payload == {"outcome": "passed"}


@pytest.mark.parametrize("digits", ["000000", "", "48291"])
async def test_wrong_or_missing_code_does_not_activate(
    async_session, unverified_number, seed_check_call, last_check_event, digits
):
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    assert (
        await finish_check(
            async_session, CheckArrived(unverified_number.id, check.id), digits, now=NOW
        )
        is False
    )
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "code_not_received"}


async def test_check_arrival_on_other_trunk_does_not_activate(
    async_session, unverified_number, seed_check_call, last_check_event
):
    check = await seed_check_call(
        unverified_number, status="ringing", requested_at=utcnow()
    )
    out = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_shared")
    )
    assert out == Rejected("pbx_check_wrong_trunk")
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "not_authenticated"}


async def test_check_arrival_on_the_right_trunk_after_a_wrong_one(
    async_session, unverified_number, seed_check_call, last_check_event
):
    """A wrong-trunk INVITE does not end the check: the right one still
    arrives and its code still counts."""
    check = await seed_check_call(
        unverified_number, status="ringing", requested_at=utcnow()
    )
    wrong = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_shared")
    )
    assert wrong == Rejected("pbx_check_wrong_trunk")
    right = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx")
    )
    assert right == CheckArrived(unverified_number.id, check.id)
    assert (await last_check_event(check)).payload == {"outcome": "arrived"}


async def test_check_arrival_without_trunk_id_does_not_activate(
    async_session, unverified_number, seed_check_call, last_check_event
):
    check = await seed_check_call(
        unverified_number, status="ringing", requested_at=utcnow()
    )
    out = await open_inbound_call(async_session, attrs(trunk_id=""))
    assert out == Rejected("pbx_check_wrong_trunk")
    assert (await last_check_event(check)).payload == {"outcome": "not_authenticated"}


async def test_check_arrival_on_deleted_connection_does_not_activate(
    async_session, unverified_number, seed_check_call, last_check_event
):
    check = await seed_check_call(
        unverified_number, status="ringing", requested_at=utcnow()
    )
    conn = await async_session.get(SipConnection, unverified_number.sip_connection_id)
    conn.deleted_at = utcnow()
    await async_session.commit()
    out = await open_inbound_call(async_session, attrs(trunk_id="ST_in_pbx"))
    assert out == Rejected("pbx_check_wrong_trunk")
    assert (await last_check_event(check)).payload == {"outcome": "not_authenticated"}


async def test_arrival_without_check_in_flight_is_dropped(
    async_session, unverified_number
):
    out = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx")
    )
    assert out == Rejected("unknown_number")
    assert unverified_number.provisioning_state == "unverified"
    assert await count_rows(async_session, Call) == 0


async def test_arrival_after_window_is_dropped(
    async_session, unverified_number, seed_check_call
):
    check = await seed_check_call(
        unverified_number,
        status="ringing",
        requested_at=utcnow() - CHECK_WINDOW - timedelta(seconds=1),
    )
    out = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx")
    )
    assert out == Rejected("unknown_number")
    assert await check_events(async_session, check) == []


async def test_arrival_for_finished_check_is_dropped(
    async_session, unverified_number, seed_check_call
):
    check = await seed_check_call(
        unverified_number, status="no_answer", requested_at=utcnow()
    )
    out = await open_inbound_call(
        async_session, attrs(dialed="+14155550100", trunk_id="ST_in_pbx")
    )
    assert out == Rejected("unknown_number")
    assert await check_events(async_session, check) == []


async def test_check_counts_one_code_per_check_call(
    async_session, unverified_number, seed_check_call
):
    """A second INVITE during the same check is not a second guess."""
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "000000", now=NOW) is False
    assert await finish_check(async_session, arrived, "482913", now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"
    assert await check_events(async_session, check) == [
        {"outcome": "code_not_received"}
    ]


async def test_a_passed_check_is_not_counted_twice(
    async_session, unverified_number, seed_check_call
):
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is True
    later = NOW + timedelta(seconds=5)
    assert await finish_check(async_session, arrived, "482913", now=later) is False
    assert unverified_number.acquired_at == NOW
    assert await check_events(async_session, check) == [{"outcome": "passed"}]


async def test_right_code_never_activates_a_removed_number(
    async_session, unverified_number, seed_check_call
):
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    unverified_number.provisioning_state = "released"
    unverified_number.released_at = utcnow()
    await async_session.commit()
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is False
    assert unverified_number.provisioning_state == "released"
    assert unverified_number.acquired_at is None


async def test_right_code_of_another_numbers_check_does_not_activate(
    async_session, unverified_number, seed_check_call
):
    check = await seed_check_call(
        unverified_number,
        status="in_progress",
        requested_at=utcnow(),
        number_id=uuid.uuid4(),
    )
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"


async def test_check_call_without_code_hash_does_not_activate(
    async_session, unverified_number, seed_check_call, last_check_event
):
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow(), code=None
    )
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"
    assert (await last_check_event(check)).payload == {"outcome": "code_not_received"}


async def test_customer_call_with_forged_check_metadata_is_not_a_check(
    async_session, unverified_number, seed_check_call
):
    """A call from the customer's own number carrying check keys (and a hash
    of a code the customer chose) never passes the check."""
    forged = await seed_check_call(
        unverified_number,
        status="in_progress",
        requested_at=utcnow(),
        code="123456",
        from_pool=False,
    )
    out = await open_inbound_call(async_session, attrs(trunk_id="ST_in_pbx"))
    assert out == Rejected("unknown_number")
    arrived = CheckArrived(unverified_number.id, forged.id)
    assert await finish_check(async_session, arrived, "123456", now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"
    assert await check_events(async_session, forged) == []


async def test_call_to_another_number_is_not_a_check(
    async_session, unverified_number, seed_check_call
):
    other = await seed_check_call(
        unverified_number,
        status="in_progress",
        requested_at=utcnow(),
        to_e164="+14155550142",
    )
    out = await open_inbound_call(async_session, attrs(trunk_id="ST_in_pbx"))
    assert out == Rejected("unknown_number")
    arrived = CheckArrived(unverified_number.id, other.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"


async def test_check_without_secret_key_never_activates(
    async_session, unverified_number, seed_check_call, monkeypatch
):
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    monkeypatch.setattr(settings, "hail_provider_secret_key", "")
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is False
    assert unverified_number.provisioning_state == "unverified"


async def test_shared_trunk_is_accepted_while_numbers_move(
    async_session, monkeypatch
) -> None:
    """A carrier with its own trunk: a number not moved yet still arrives on
    the shared trunk and is answered; another carrier's trunk is refused."""
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_shared")
    )
    assert isinstance(outcome, inbound_calls.Accepted)


async def test_another_carriers_trunk_is_refused_while_numbers_move(
    async_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", "ST_in_tw")
    monkeypatch.setattr(settings, "livekit_telnyx_sip_inbound_trunk_id", "ST_in_tx")
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_tx")
    )
    assert isinstance(outcome, inbound_calls.Rejected)


SECRET = "s3cr3t-value-for-tests_0123456789abcdef"


async def test_twilio_call_with_the_secret_is_answered(
    async_session, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(sip_secret=SECRET)
    )
    assert isinstance(outcome, inbound_calls.Accepted)


@pytest.mark.parametrize("sent", ["", "wrong", SECRET[:-1], "é" * 40])
async def test_twilio_call_without_the_secret_is_refused(
    async_session, monkeypatch, caplog, sent
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    await _seed(async_session)
    with caplog.at_level("INFO", logger="hailhq.core.inbound_calls"):
        outcome = await inbound_calls.open_inbound_call(
            async_session, _attrs(sip_secret=sent)
        )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "bad_secret"
    assert outcome.call_id is None
    # Maybe forged: nothing in the customer's call log, no webhook.
    assert (await async_session.execute(select(Call))).scalars().all() == []
    assert (await async_session.execute(select(CallEvent))).scalars().all() == []
    assert await _deliveries(async_session, "call.failed") == []
    warnings = [r.getMessage() for r in caplog.records if r.levelname == "WARNING"]
    why = "wrong" if sent else "missing"
    assert warnings == [
        f"inbound call to {DIALED} refused: X-Hail-Secret header {why} (carrier=twilio)"
    ]
    assert not any(SECRET in r.getMessage() for r in caplog.records)
    assert SECRET not in repr(_attrs(sip_secret=SECRET))


@pytest.mark.parametrize(
    ("trunk_id", "capabilities"),
    [("ST_in_tw", ["sms"]), ("ST_in_tx", ["voice", "sms"])],
    ids=["no_voice", "wrong_trunk"],
)
async def test_secret_is_checked_before_the_trunk_and_voice_refusals(
    async_session, monkeypatch, trunk_id, capabilities
) -> None:
    """A forged call to a Twilio number that would be refused anyway writes
    no failed row either: the secret check runs first."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    await _seed(async_session, capabilities=capabilities)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id=trunk_id)
    )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "bad_secret"
    assert (await async_session.execute(select(Call))).scalars().all() == []


@pytest.fixture(autouse=True)
def fresh_secret_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The once-per-process log state starts empty in every test."""
    monkeypatch.setattr(inbound_calls, "_secret_off_logged", False)
    monkeypatch.setattr(inbound_calls, "_shared_hint_logged", set())


SHARED_HINT = (
    f"inbound call to {DIALED}: Twilio number still on the shared trunk; move "
    "it (scripts/ops/sip_trunks.py move --carrier twilio)"
)


async def test_twilio_number_on_the_shared_trunk_says_move_it(
    async_session, monkeypatch, caplog
) -> None:
    """The shared trunk does not map the header: the call is refused, and
    after the failed compare the log says (once per number, at INFO) that
    the number must move to the Twilio trunk."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    await _seed(async_session)
    with caplog.at_level("INFO", logger="hailhq.core.inbound_calls"):
        for _ in range(2):
            outcome = await inbound_calls.open_inbound_call(
                async_session, _attrs(trunk_id="ST_shared")
            )
            assert isinstance(outcome, inbound_calls.Rejected)
            assert outcome.reason == "bad_secret"
            # A real call here carries no secret either, so it cannot be told
            # from a forged one: no row, only the ops hint.
            assert outcome.call_id is None
    assert (await async_session.execute(select(Call))).scalars().all() == []
    assert await _deliveries(async_session, "call.failed") == []
    messages = [(r.levelname, r.getMessage()) for r in caplog.records]
    refused = (
        "WARNING",
        (
            f"inbound call to {DIALED} refused: X-Hail-Secret header missing "
            "(carrier=twilio)"
        ),
    )
    assert messages.count(("INFO", SHARED_HINT)) == 1
    assert messages.count(refused) == 2
    # The hint follows the failed compare.
    assert messages.index(refused) < messages.index(("INFO", SHARED_HINT))


async def test_valid_secret_on_the_shared_trunk_gives_no_move_hint(
    async_session, monkeypatch, caplog
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    await _seed(async_session)
    with caplog.at_level("INFO", logger="hailhq.core.inbound_calls"):
        outcome = await inbound_calls.open_inbound_call(
            async_session, _attrs(trunk_id="ST_shared", sip_secret=SECRET)
        )
    assert isinstance(outcome, inbound_calls.Accepted)
    assert SHARED_HINT not in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize(
    ("shared", "twilio", "secret", "why"),
    [
        ("", "", SECRET, "is set but no Twilio inbound trunk"),
        ("ST_in_tw", "ST_in_tw", SECRET, "is set but no Twilio inbound trunk"),
        ("", "ST_in_tw", "short&bad", "is not 20 or more letters, digits, - or _"),
    ],
    ids=["no_twilio_trunk", "twilio_is_shared", "malformed"],
)
async def test_secret_that_cannot_work_turns_the_check_off(
    async_session, monkeypatch, caplog, shared, twilio, secret, why
) -> None:
    """Never a startup failure: the check is off, with one ERROR per
    process, and calls go on as without the secret."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", secret)
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", shared)
    monkeypatch.setattr(settings, "livekit_twilio_sip_inbound_trunk_id", twilio)
    await _seed(async_session)
    with caplog.at_level("INFO", logger="hailhq.core.inbound_calls"):
        for i in range(2):
            outcome = await inbound_calls.open_inbound_call(
                async_session,
                _attrs(room_name=f"hail-in-{i}", provider_call_sid=f"sip-call-{i}"),
            )
            # The secret refuses nothing (the trunk check may, with no
            # Twilio trunk at all).
            assert getattr(outcome, "reason", None) != "bad_secret"
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert errors == [f"HAIL_SIP_INBOUND_SECRET {why}: secret check off"]
    assert not any(secret in r.getMessage() for r in caplog.records)


async def test_empty_setting_checks_no_secret(
    async_session, monkeypatch, caplog
) -> None:
    """Self-host default: no secret, no check. The header is still logged as
    present or missing, which the rollout's first test call reads."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", "")
    await _seed(async_session)
    with caplog.at_level("INFO", logger="hailhq.core.inbound_calls"):
        outcome = await inbound_calls.open_inbound_call(async_session, _attrs())
    assert isinstance(outcome, inbound_calls.Accepted)
    assert f"inbound twilio call to {DIALED}: X-Hail-Secret header missing" in [
        r.getMessage() for r in caplog.records
    ]


@pytest.mark.parametrize("carrier", ["telnyx", "didww"])
async def test_other_carriers_need_no_secret(
    async_session, monkeypatch, carrier
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_didww_sip_inbound_trunk_id", "ST_in_dw")
    trunk = {"telnyx": "ST_in_tx", "didww": "ST_in_dw"}[carrier]
    await _seed(async_session, provider=carrier)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id=trunk)
    )
    assert isinstance(outcome, inbound_calls.Accepted)


async def test_wrong_trunk_keeps_its_failed_row_after_a_valid_secret(
    async_session, monkeypatch
) -> None:
    """Real misrouting (valid secret, wrong trunk) stays visible."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_tx", sip_secret=SECRET)
    )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"
    calls = (await async_session.execute(select(Call))).scalars().all()
    assert [c.end_reason for c in calls] == ["carrier_route_failed"]


# Connected (pbx) numbers next to the Twilio secret check: a pbx call is
# authenticated by its connection trunk's digest login, never by the header,
# and only its connection trunk is accepted.


@pytest.mark.parametrize("sent", ["", "wrong"])
async def test_pbx_call_needs_no_twilio_secret(
    async_session, monkeypatch, sent
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    await _pbx_seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_pbx", sip_secret=sent)
    )
    assert isinstance(outcome, inbound_calls.Accepted)


async def test_check_arrival_needs_no_twilio_secret(
    async_session, monkeypatch, unverified_number, seed_check_call
) -> None:
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    check = await seed_check_call(
        unverified_number, status="ringing", requested_at=utcnow()
    )
    out = await open_inbound_call(async_session, attrs(trunk_id="ST_in_pbx"))
    assert out == CheckArrived(unverified_number.id, check.id)


@pytest.mark.parametrize("trunk_id", ["ST_shared", "ST_in_tw", "ST_in_tx"])
async def test_pbx_call_on_the_shared_or_a_carrier_trunk_is_refused(
    async_session, monkeypatch, trunk_id
) -> None:
    """The shared trunk is accepted while carrier numbers move, never for a
    connected number: it does not check the connection's login."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    monkeypatch.setattr(settings, "livekit_sip_inbound_trunk_id", "ST_shared")
    await _pbx_seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id=trunk_id, sip_secret=SECRET)
    )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"


async def test_twilio_call_on_a_connection_trunk_still_needs_the_secret(
    async_session, monkeypatch
) -> None:
    """A carrier number reached through a connection trunk: no header, no
    row; with the header, the trunk check refuses it."""
    monkeypatch.setattr(settings, "hail_sip_inbound_secret", SECRET)
    await _seed(async_session)
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_pbx")
    )
    assert outcome == Rejected("bad_secret")
    assert (await async_session.execute(select(Call))).scalars().all() == []
    outcome = await inbound_calls.open_inbound_call(
        async_session, _attrs(trunk_id="ST_in_pbx", sip_secret=SECRET)
    )
    assert isinstance(outcome, inbound_calls.Rejected)
    assert outcome.reason == "carrier_route_failed"


@pytest.mark.parametrize("region,expected", [(None, "US"), ("GB", "GB")])
async def test_a_passed_check_fills_an_empty_country_only(
    async_session, unverified_number, seed_check_call, region, expected
):
    """The first number to pass fills a connection's empty country; a country
    already set stays."""
    connection = await async_session.get(
        SipConnection, unverified_number.sip_connection_id
    )
    connection.default_region = region
    await async_session.commit()
    check = await seed_check_call(
        unverified_number, status="in_progress", requested_at=utcnow()
    )
    arrived = CheckArrived(unverified_number.id, check.id)
    assert await finish_check(async_session, arrived, "482913", now=NOW) is True
    await async_session.refresh(connection)
    assert connection.default_region == expected
