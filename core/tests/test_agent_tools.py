"""Agent-tool registry: shape, availability, and executor behavior."""

from __future__ import annotations

import subprocess
import sys
import uuid

from hailhq.core.agent_tools.registry import all_tools
from hailhq.core.agent_tools.spec import ToolContext
from hailhq.core.config import settings
from hailhq.core.models import EmailDomain, PhoneNumber


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
    }
    assert tools["end_call"].risk_tier == "session_control"
    assert tools["send_dtmf"].risk_tier == "session_control"
    assert tools["list_contacts"].risk_tier == "read_only"
    assert tools["send_sms"].risk_tier == "outbound_send"
    assert tools["send_email"].risk_tier == "outbound_send"


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
