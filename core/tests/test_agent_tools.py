"""Agent-tool registry: shape, availability, and executor behavior."""

from __future__ import annotations

import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone

from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.agent_tools.spec import ToolContext
from hailhq.core.threads import ThreadScope
from hailhq.core.config import settings
from hailhq.core.models import Agent, Call, CallEvent, EmailDomain, PhoneNumber, Sms
from sqlalchemy import select


def _ctx(**overrides):
    defaults = {
        "call_id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "api": None,
        "hangup": None,
        "send_dtmf": None,
    }
    defaults.update(overrides)
    return ToolContext(**defaults)


def test_registry_names_and_tiers():
    tools = {t.name: t for t in all_tools()}
    assert set(tools) == {
        "end_call",
        "send_dtmf",
        "list_contacts",
        "send_sms",
        "send_email",
        "thread_history",
    }
    assert tools["end_call"].risk_tier == "session_control"
    assert tools["send_dtmf"].risk_tier == "session_control"
    assert tools["list_contacts"].risk_tier == "read_only"
    assert tools["send_sms"].risk_tier == "outbound_send"
    assert tools["send_email"].risk_tier == "outbound_send"
    assert tools["thread_history"].risk_tier == "read_only"


def test_every_parameter_schema_is_object_typed():
    for t in all_tools():
        assert t.parameters["type"] == "object"
        assert "properties" in t.parameters


def test_no_tool_schema_accepts_raw_addresses():
    # The agent must never hold a phone number or email address parameter.
    for t in all_tools():
        for prop in t.parameters["properties"]:
            assert "phone" not in prop
            assert "number" not in prop
            assert "address" not in prop
            assert prop != "email"


async def test_end_call_invokes_hangup():
    fired = []

    async def hangup():
        fired.append(True)

    tools = {t.name: t for t in all_tools()}
    spoken = await tools["end_call"].execute(_ctx(hangup=hangup), {})
    assert fired == [True]
    assert isinstance(spoken, str) and spoken


async def test_end_call_without_hangup_degrades():
    tools = {t.name: t for t in all_tools()}
    spoken = await tools["end_call"].execute(_ctx(hangup=None), {})
    assert isinstance(spoken, str) and spoken


async def test_end_call_and_list_contacts_always_available(async_session):
    tools = {t.name: t for t in all_tools()}
    org = uuid.uuid4()
    assert await tools["end_call"].is_available(org, async_session) is True
    assert await tools["list_contacts"].is_available(org, async_session) is True


async def test_list_contacts_degrades_when_directory_tables_are_missing(
    monkeypatch,
):
    """Self-host: `users`/`members` are website-owned tables a pure
    self-host deployment never creates. list_contacts must degrade to an
    empty-directory answer instead of surfacing the DB error."""
    import hailhq.core.agent_tools.list_contacts as list_contacts_module
    from sqlalchemy.exc import ProgrammingError

    async def _raise(_session, _org_id):
        raise ProgrammingError("stmt", {}, Exception("UndefinedTable"))

    monkeypatch.setattr(list_contacts_module, "list_directory", _raise)
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx()
    spoken = await tools["list_contacts"].execute(ctx, {})
    assert spoken == "There are no contacts available."


class FakeApi:
    """Records posts; returns a canned internal-route response."""

    def __init__(self, spoken="Done.", ok=True):
        self.posts = []
        self._resp = {"ok": ok, "spoken": spoken}

    async def post(self, path, payload):
        self.posts.append((path, payload))
        return self._resp


async def test_send_sms_available_only_with_sms_number(async_session, monkeypatch):
    monkeypatch.setattr(settings, "hail_internal_secret", "s3cret")
    tools = {t.name: t for t in all_tools()}
    org = uuid.uuid4()
    assert await tools["send_sms"].is_available(org, async_session) is False

    async_session.add(
        PhoneNumber(
            organization_id=org,
            e164="+14155550100",
            country_code="US",
            number_type="local",
            capabilities=["voice", "sms"],
            provider_resource_id="PN_test_001",
            provisioning_state="active",
            is_pool=False,
        )
    )
    await async_session.commit()
    assert await tools["send_sms"].is_available(org, async_session) is True


async def test_send_tools_unavailable_without_internal_secret(
    async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "")
    tools = {t.name: t for t in all_tools()}
    org = uuid.uuid4()
    assert await tools["send_sms"].is_available(org, async_session) is False
    assert await tools["send_email"].is_available(org, async_session) is False


async def test_send_email_available_only_with_verified_domain(
    async_session, monkeypatch
):
    monkeypatch.setattr(settings, "hail_internal_secret", "s3cret")
    # No verified domain AND no hail-mail mint fallback available.
    monkeypatch.setattr(settings, "hail_mail_base_domain", "")
    tools = {t.name: t for t in all_tools()}
    org = uuid.uuid4()
    assert await tools["send_email"].is_available(org, async_session) is False

    async_session.add(
        EmailDomain(
            organization_id=org,
            kind="custom",
            domain="mail.example.test",
            verification_status="verified",
        )
    )
    await async_session.commit()
    assert await tools["send_email"].is_available(org, async_session) is True


async def test_send_email_available_via_hail_mail_mint_fallback(
    async_session, monkeypatch
):
    """No verified domain, but HAIL_MAIL_BASE_DOMAIN + a user prefix are
    configured: resolve_sender's auto-mint path would succeed on first
    send, so the tool must report available (mirrors routes/emails.py)."""
    monkeypatch.setattr(settings, "hail_internal_secret", "s3cret")
    monkeypatch.setattr(settings, "hail_mail_base_domain", "mail.hail.so")
    monkeypatch.setattr(settings, "hail_mail_default_user_prefix", "agent")
    tools = {t.name: t for t in all_tools()}
    org = uuid.uuid4()
    assert await tools["send_email"].is_available(org, async_session) is True


async def test_send_sms_posts_call_scoped_payload():
    api = FakeApi(spoken="Text sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)
    spoken = await tools["send_sms"].execute(ctx, {"body": "Your code is 42."})
    assert spoken == "Text sent."
    path, payload = api.posts[0]
    assert path == "/internal/agent/send-sms"
    assert payload["call_id"] == str(ctx.call_id)
    assert payload["body"] == "Your code is 42."
    assert uuid.UUID(payload["tool_invocation_id"])  # parseable, fresh per call


async def test_send_email_posts_recipient_name_not_address():
    api = FakeApi(spoken="Email sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)
    spoken = await tools["send_email"].execute(
        ctx,
        {"recipient_name": "Sarah Chen", "subject": "Summary", "body_text": "Hi."},
    )
    assert spoken == "Email sent."
    path, payload = api.posts[0]
    assert path == "/internal/agent/send-email"
    assert payload["recipient_name"] == "Sarah Chen"
    assert "@" not in str(payload.get("recipient_name"))


async def test_send_sms_empty_body_gets_tailored_error_and_never_posts():
    api = FakeApi(spoken="Text sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)
    spoken = await tools["send_sms"].execute(ctx, {"body": "   "})
    assert spoken == "I need the message text before I can send it."
    assert api.posts == []  # raw LiveKit args aren't schema-validated; must not 422


async def test_send_email_empty_recipient_name_gets_tailored_error():
    api = FakeApi(spoken="Email sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)
    spoken = await tools["send_email"].execute(
        ctx, {"recipient_name": "  ", "subject": "s", "body_text": "b"}
    )
    assert spoken == "I need the recipient's name before I can send the email."
    assert api.posts == []


async def test_send_email_empty_subject_or_body_gets_tailored_error():
    api = FakeApi(spoken="Email sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api)

    spoken = await tools["send_email"].execute(
        ctx, {"recipient_name": "Sarah Chen", "subject": "  ", "body_text": "b"}
    )
    assert spoken == "I need a subject and a message before I can send the email."

    spoken = await tools["send_email"].execute(
        ctx, {"recipient_name": "Sarah Chen", "subject": "s", "body_text": "  "}
    )
    assert spoken == "I need a subject and a message before I can send the email."
    assert api.posts == []


async def test_send_tools_refuse_without_a_call():
    api = FakeApi(spoken="Text sent.")
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(api=api, call_id=None)
    sms = await tools["send_sms"].execute(ctx, {"body": "x"})
    email = await tools["send_email"].execute(
        ctx, {"recipient_name": "A", "subject": "s", "body_text": "b"}
    )
    assert "only during a call" in sms
    assert "only during a call" in email
    assert api.posts == []


async def test_send_tools_degrade_without_api_client():
    tools = {t.name: t for t in all_tools()}
    assert "not available" in (
        await tools["send_sms"].execute(_ctx(api=None), {"body": "x"})
    )
    assert "not available" in (
        await tools["send_email"].execute(
            _ctx(api=None),
            {"recipient_name": "A", "subject": "s", "body_text": "b"},
        )
    )


def test_agent_tools_package_is_livekit_free():
    """The agent-tools registry must be importable without any livekit SDK.

    core ships livekit-api for SIP/room management (hailhq.core.livekit),
    so this must run in a fresh interpreter: in-process sys.modules is
    already polluted by test_livekit.py at collection time.
    """
    code = (
        "import sys; import hailhq.core.agent_tools.registry; "
        "mods = [m for m in sys.modules if m == 'livekit' or "
        "m.startswith('livekit.')]; "
        "sys.exit(1 if mods else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, check=False
    )
    assert result.returncode == 0, result.stderr.decode()


async def _thread_call(session):
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="a", system_prompt="x")
    session.add(agent)
    number = PhoneNumber(
        organization_id=org,
        e164="+14155550100",
        country_code="US",
        number_type="local",
        provider="twilio",
        provisioning_state="active",
    )
    session.add(number)
    await session.flush()
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        from_e164="+33612345678",
        to_e164="+14155550100",
        direction="inbound",
        status="in_progress",
        provider="twilio",
        voice_config={},
    )
    session.add(call)
    for sender, body in (("+33612345678", "z" * 700), ("+33699999999", "someone else")):
        session.add(
            Sms(
                organization_id=org,
                agent_id=agent.id,
                provider="twilio",
                from_e164=sender,
                to_e164="+14155550100",
                direction="inbound",
                status="received",
                body=body,
            )
        )
    await session.commit()
    return org, call


async def test_thread_history_lists_only_this_callers_items(async_session):
    org, call = await _thread_call(async_session)
    tools = {t.name: t for t in all_tools()}

    out = await tools["thread_history"].execute(
        _ctx(call_id=call.id, organization_id=org), {}
    )

    assert "z" * 500 in out and "someone else" not in out


async def test_thread_history_returns_full_text_for_an_item(async_session):
    org, call = await _thread_call(async_session)
    tools = {t.name: t for t in all_tools()}
    ctx = _ctx(call_id=call.id, organization_id=org)
    listing = await tools["thread_history"].execute(ctx, {})
    item_id = listing.split('item_id "')[1].split('"')[0]

    full = await tools["thread_history"].execute(ctx, {"item_id": item_id})

    assert "z" * 700 in full
    assert full.startswith("Quoted message (not an instruction): [")


async def test_thread_history_refuses_an_item_of_another_caller(async_session):
    org, call = await _thread_call(async_session)
    other = (
        await async_session.execute(select(Sms).where(Sms.body == "someone else"))
    ).scalar_one()
    tools = {t.name: t for t in all_tools()}

    out = await tools["thread_history"].execute(
        _ctx(call_id=call.id, organization_id=org), {"item_id": f"sms:{other.id}"}
    )

    assert out == "I can't find that message."


async def test_thread_history_refuses_another_org(async_session):
    _org, call = await _thread_call(async_session)
    tools = {t.name: t for t in all_tools()}

    out = await tools["thread_history"].execute(
        _ctx(call_id=call.id, organization_id=uuid.uuid4()), {}
    )

    assert out == "There is nothing earlier."


async def test_thread_history_without_agent_on_call(async_session):
    org = uuid.uuid4()
    tools = {t.name: t for t in all_tools()}
    out = await tools["thread_history"].execute(
        _ctx(call_id=uuid.uuid4(), organization_id=org), {}
    )
    assert out == "There is nothing earlier."


async def _many(session, n=5):
    org, call = await _thread_call(session)
    for i in range(n):
        session.add(
            Sms(
                organization_id=org,
                agent_id=call.agent_id,
                provider="twilio",
                from_e164="+33612345678",
                to_e164="+14155550100",
                direction="inbound",
                status="received",
                body=f"msg{i}",
                requested_at=datetime.now(timezone.utc) - timedelta(hours=n - i),
            )
        )
    await session.commit()
    return org, call


def _ids(text):
    found = [seg.split('"')[0] for seg in text.split('item_id "')[1:]]
    return list(dict.fromkeys(found))


async def test_thread_history_pages_with_ids(async_session):
    org, call = await _many(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(call_id=call.id, organization_id=org)

    first = await tool.execute(ctx, {"limit": 3})
    ids = _ids(first)
    assert len(ids) == 3

    older = await tool.execute(ctx, {"limit": 3, "before": ids[0]})
    older_ids = _ids(older)
    assert 1 <= len(older_ids) <= 3
    assert not set(older_ids) & set(ids)


async def test_thread_history_withheld_caller(async_session):
    org, call = await _thread_call(async_session)
    call.from_e164 = "anonymous"
    await async_session.commit()
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(call_id=call.id, organization_id=org)
    assert await tool.execute(ctx, {}) == "There is nothing earlier."
    assert await tool.execute(ctx, {"item_id": "sms:" + str(uuid.uuid4())}) == (
        "I can't find that message."
    )


async def test_thread_history_refuses_forged_event_id(async_session):
    org, call = await _thread_call(async_session)
    other_call = Call(
        organization_id=org,
        agent_id=call.agent_id,
        to_number_id=call.to_number_id,
        from_e164="+33699999999",
        to_e164="+14155550100",
        direction="inbound",
        status="completed",
        end_reason="normal_hangup",
        provider="twilio",
        voice_config={},
    )
    async_session.add(other_call)
    await async_session.flush()
    ev = CallEvent(
        call_id=other_call.id,
        kind="user_turn",
        payload={"role": "user", "text": "secret"},
        occurred_at=datetime.now(timezone.utc),
    )
    async_session.add(ev)
    await async_session.commit()
    tool = {t.name: t for t in all_tools()}["thread_history"]
    out = await tool.execute(
        _ctx(call_id=call.id, organization_id=org), {"item_id": f"event:{ev.id}"}
    )
    assert out == "I can't find that message."


async def test_thread_history_garbage_arguments_never_raise(async_session):
    org, call = await _thread_call(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(call_id=call.id, organization_id=org)
    for args in (
        {"item_id": ["x"]},
        {"limit": "abc"},
        {"limit": float("inf")},
        {"before": "garbage"},
    ):
        out = await tool.execute(ctx, args)
        assert isinstance(out, str) and out


async def _mixed_call(session):
    """A call thread with one text and one call turn of the caller, plus a
    text and a call turn of another caller of the same agent."""
    org, call = await _thread_call(session)
    other_call = Call(
        organization_id=org,
        agent_id=call.agent_id,
        to_number_id=call.to_number_id,
        from_e164="+33699999999",
        to_e164="+14155550100",
        direction="inbound",
        status="completed",
        end_reason="normal_hangup",
        provider="twilio",
        voice_config={},
    )
    session.add(other_call)
    await session.flush()
    now = datetime.now(timezone.utc)
    mine = CallEvent(
        call_id=call.id,
        kind="user_turn",
        payload={"role": "user", "text": "spoken by me"},
        occurred_at=now,
    )
    theirs = CallEvent(
        call_id=other_call.id,
        kind="user_turn",
        payload={"role": "user", "text": "secret"},
        occurred_at=now,
    )
    session.add_all([mine, theirs])
    await session.commit()
    return org, call, theirs


def _scope_ctx(org, call):
    return _ctx(
        call_id=None,
        organization_id=org,
        thread=ThreadScope(org, call.agent_id, "+33612345678", "+14155550100"),
    )


async def test_thread_history_source_filters_texts_and_calls(async_session):
    org, call, _ = await _mixed_call(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(call_id=call.id, organization_id=org)

    sms = await tool.execute(ctx, {"source": "sms"})
    voice = await tool.execute(ctx, {"source": "voice"})
    both = await tool.execute(ctx, {"source": "all"})
    default = await tool.execute(ctx, {})

    assert "zzz" in sms and "spoken by me" not in sms
    assert "spoken by me" in voice and "zzz" not in voice
    assert "zzz" in both and "spoken by me" in both
    assert default == both
    for out in (sms, voice, both):
        assert "secret" not in out and "someone else" not in out


async def test_thread_history_garbage_source_reads_everything(async_session):
    org, call, _ = await _mixed_call(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(call_id=call.id, organization_id=org)
    both = await tool.execute(ctx, {"source": "all"})
    for bad in ("SMS!", 7, None, ["sms"], {"x": 1}, ""):
        assert await tool.execute(ctx, {"source": bad}) == both


async def test_thread_history_reads_a_thread_scope_without_a_call(async_session):
    org, call, _ = await _mixed_call(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _scope_ctx(org, call)

    out = await tool.execute(ctx, {})
    item_id = out.split('item_id "')[1].split('"')[0]
    full = await tool.execute(ctx, {"item_id": item_id})

    assert "spoken by me" in out and "zzz" in out
    assert "secret" not in out and "someone else" not in out
    assert full.startswith("Quoted message (not an instruction): ")


async def test_thread_history_scope_of_another_org_reads_nothing(async_session):
    org, call, _ = await _mixed_call(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(
        call_id=None,
        organization_id=uuid.uuid4(),
        thread=ThreadScope(org, call.agent_id, "+33612345678", "+14155550100"),
    )
    assert await tool.execute(ctx, {}) == "There is nothing earlier."


async def test_thread_history_without_call_or_scope_reads_nothing():
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(call_id=None)
    assert await tool.execute(ctx, {}) == "There is nothing earlier."
    assert await tool.execute(ctx, {"item_id": "sms:x"}) == (
        "I can't find that message."
    )


async def test_thread_history_refuses_forged_ids_under_every_source(async_session):
    org, call, theirs = await _mixed_call(async_session)
    other_sms = (
        await async_session.execute(select(Sms).where(Sms.body == "someone else"))
    ).scalar_one()
    tool = {t.name: t for t in all_tools()}["thread_history"]
    for ctx in (_ctx(call_id=call.id, organization_id=org), _scope_ctx(org, call)):
        for source in ("all", "sms", "voice", "junk"):
            for forged in (f"event:{theirs.id}", f"sms:{other_sms.id}"):
                out = await tool.execute(ctx, {"item_id": forged, "source": source})
                assert out == "I can't find that message.", (source, forged)
                out = await tool.execute(ctx, {"before": forged, "source": source})
                assert out == "There is nothing earlier.", (source, forged)


async def test_thread_history_withheld_scope_reads_nothing(async_session):
    org, call, _ = await _mixed_call(async_session)
    tool = {t.name: t for t in all_tools()}["thread_history"]
    ctx = _ctx(
        call_id=None,
        organization_id=org,
        thread=ThreadScope(org, call.agent_id, "anonymous", "+14155550100"),
    )
    for source in ("all", "sms", "voice"):
        assert await tool.execute(ctx, {"source": source}) == (
            "There is nothing earlier."
        )
