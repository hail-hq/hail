"""Email worker: builds the chat from the thread, posts the reply, retries."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.agent_tools import send_email, send_sms
from hailhq.core.agent_tools.email_run import email_run_tools
from hailhq.core.agent_tools.spec import NO_CALL, ToolContext
from hailhq.core.config import settings
from hailhq.core.email_agent import MAX_REPLIES_PER_THREAD
from hailhq.core.models import Agent, Email, EmailDomain, OrganizationMember, User
from hailhq.voicebot import emailbot
from hailhq.voicebot.pipeline import ProviderKeyError

from ._fakes import FakeLLM, ScriptedLLM

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)
ADDRESS = "amber-heron-42+acme@mail.hail.so"
MEMBER = "ada@example.com"


@pytest.fixture(autouse=True)
def _internal_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "hail_internal_secret", "test-secret")


async def _seed(
    session, *, scope="members", tools=None, sender=MEMBER, dmarc="PASS", earlier=False
):
    org = uuid.uuid4()
    agent = Agent(
        organization_id=org, name="Inbox", system_prompt="Help the team.", tools=tools
    )
    session.add(agent)
    await session.flush()
    domain = EmailDomain(
        organization_id=org,
        kind="hail_mail",
        domain=ADDRESS,
        local_prefix_user="amber-heron-42",
        local_prefix_org="acme",
        verification_status="verified",
        provider="ses",
        verified_at=NOW,
        email_agent_id=agent.id,
        email_agent_scope=scope,
    )
    session.add(domain)
    await session.flush()
    uid = uuid.uuid4()
    session.add(
        User(id=uid, name="Ada", email=MEMBER, email_verified=True, created_at=NOW)
    )
    session.add(
        OrganizationMember(
            id=uuid.uuid4(),
            user_id=uid,
            organization_id=org,
            role="member",
            created_at=NOW,
        )
    )
    row = Email(
        organization_id=org,
        email_domain_id=domain.id,
        email_domain_kind="hail_mail",
        direction="inbound",
        from_address=sender,
        to_addresses=[ADDRESS],
        subject="Question",
        body_text="Can you help?",
        status="received",
        message_id="<one@example.com>",
        references_ids=["<zero@example.com>"] if earlier else None,
        dmarc_verdict=dmarc,
        from_verified=dmarc == "PASS",
        agent_id=agent.id,
        agent_reply_state="pending",
    )
    session.add(row)
    if earlier:
        session.add(
            Email(
                organization_id=org,
                email_domain_id=domain.id,
                email_domain_kind="hail_mail",
                direction="inbound",
                from_address=sender,
                to_addresses=[ADDRESS],
                subject="Question",
                body_text="Earlier private note",
                status="received",
                message_id="<zero@example.com>",
                dmarc_verdict="PASS",
                from_verified=True,
            )
        )
    await session.commit()
    return row.id


def _use(monkeypatch: pytest.MonkeyPatch, llm) -> None:
    async def fake_resolve(_org, *, skip_llm=False):
        return {}

    monkeypatch.setattr(emailbot, "resolve_org_configs", fake_resolve)
    monkeypatch.setattr(emailbot, "build_llm", lambda cfg, org=None: llm)


async def _state(session, email_id) -> str | None:
    session.expire_all()
    return (await session.get(Email, email_id)).agent_reply_state


async def test_reply_once_posts_the_model_text(async_session, monkeypatch) -> None:
    email_id = await _seed(async_session)
    _use(monkeypatch, FakeLLM(reply="Happy to help."))
    api = SimpleNamespace(post=AsyncMock(return_value={"ok": True, "state": "done"}))

    assert await emailbot.reply_once(api) is True

    api.post.assert_awaited_once_with(
        "/internal/agent/reply-email",
        {"email_id": str(email_id), "body": "Happy to help."},
    )
    assert await _state(async_session, email_id) == "done"
    assert await emailbot.reply_once(api) is False


async def test_nothing_waiting_returns_false(async_session) -> None:
    assert await emailbot.reply_once(SimpleNamespace(post=AsyncMock())) is False


async def test_skipped_state_is_recorded(async_session, monkeypatch) -> None:
    email_id = await _seed(async_session)
    _use(monkeypatch, FakeLLM(reply="Hi"))
    api = SimpleNamespace(
        post=AsyncMock(
            return_value={"ok": False, "state": "skipped", "reason": "thread_cap"}
        )
    )
    await emailbot.reply_once(api)
    assert await _state(async_session, email_id) == "skipped"


async def test_a_provider_error_fails_without_posting(
    async_session, monkeypatch
) -> None:
    email_id = await _seed(async_session)

    async def boom(_org, *, skip_llm=False):
        raise ProviderKeyError("bad key")

    monkeypatch.setattr(emailbot, "resolve_org_configs", boom)
    api = SimpleNamespace(post=AsyncMock())
    await emailbot.reply_once(api)
    api.post.assert_not_awaited()
    assert await _state(async_session, email_id) == "failed"


async def test_a_model_error_goes_back_for_a_retry(async_session, monkeypatch) -> None:
    email_id = await _seed(async_session)

    async def boom(_org, *, skip_llm=False):
        raise RuntimeError("model down")

    monkeypatch.setattr(emailbot, "resolve_org_configs", boom)
    await emailbot.reply_once(SimpleNamespace(post=AsyncMock()))
    async_session.expire_all()
    row = await async_session.get(Email, email_id)
    assert row.agent_reply_state == "pending"
    assert row.agent_reply_attempts == 1
    assert row.agent_reply_available_at is not None


async def test_a_lost_post_goes_back_for_a_retry(async_session, monkeypatch) -> None:
    email_id = await _seed(async_session)
    _use(monkeypatch, FakeLLM(reply="Hi"))
    api = SimpleNamespace(post=AsyncMock(side_effect=TimeoutError()))
    await emailbot.reply_once(api)
    assert await _state(async_session, email_id) == "pending"


async def test_an_empty_reply_is_skipped(async_session, monkeypatch) -> None:
    email_id = await _seed(async_session)
    _use(monkeypatch, FakeLLM(reply="   "))
    api = SimpleNamespace(post=AsyncMock())
    await emailbot.reply_once(api)
    api.post.assert_not_awaited()
    assert await _state(async_session, email_id) == "skipped"


async def test_the_model_reads_the_mail_as_a_labeled_message(
    async_session, monkeypatch
) -> None:
    await _seed(async_session)
    llm = ScriptedLLM([("text", "Hi")])
    _use(monkeypatch, llm)
    await emailbot.reply_once(
        SimpleNamespace(post=AsyncMock(return_value={"state": "done"}))
    )
    text = [str(i.text_content) for i in llm.calls[0]["items"]]
    assert "verified member" in text[0]
    assert text[1].startswith(f"Email from {MEMBER}")


async def test_a_proven_outside_sender_is_framed_and_keeps_tools(
    async_session, monkeypatch
) -> None:
    await _seed(async_session, scope="anyone", sender="stranger@example.net")
    llm = ScriptedLLM([("text", "Hi")])
    _use(monkeypatch, llm)
    await emailbot.reply_once(
        SimpleNamespace(post=AsyncMock(return_value={"state": "done"}))
    )
    assert "outside the organization" in str(llm.calls[0]["items"][0].text_content)
    assert llm.calls[0]["tools"]


async def test_an_unverified_sender_gets_no_tools_and_no_history(
    async_session, monkeypatch
) -> None:
    await _seed(
        async_session,
        scope="anyone",
        sender="stranger@example.net",
        dmarc="GRAY",
        earlier=True,
    )
    llm = ScriptedLLM([("text", "Hi")])
    _use(monkeypatch, llm)
    api = SimpleNamespace(post=AsyncMock(return_value={"state": "done"}))
    await emailbot.reply_once(api)
    call = llm.calls[0]
    assert not call["tools"]
    assert len(llm.calls) == 1
    text = " ".join(str(i.text_content) for i in call["items"])
    assert "may be forged" in text
    assert "Earlier private note" not in text
    assert api.post.await_args.args[0] == "/internal/agent/reply-email"


async def test_a_proven_member_reads_the_history(async_session, monkeypatch) -> None:
    await _seed(async_session, earlier=True)
    llm = ScriptedLLM([("text", "Hi")])
    _use(monkeypatch, llm)
    await emailbot.reply_once(
        SimpleNamespace(post=AsyncMock(return_value={"state": "done"}))
    )
    text = " ".join(str(i.text_content) for i in llm.calls[0]["items"])
    assert "Earlier private note" in text


async def test_the_cap_is_read_before_the_model_is_paid(
    async_session, monkeypatch
) -> None:
    email_id = await _seed(async_session)
    row = await async_session.get(Email, email_id)
    for _ in range(MAX_REPLIES_PER_THREAD):
        async_session.add(
            Email(
                organization_id=row.organization_id,
                email_domain_id=row.email_domain_id,
                agent_id=row.agent_id,
                from_address=ADDRESS,
                to_addresses=["other@example.com"],
                subject="Note",
                body_text="Hello",
                status="sent",
                metadata_={"thread_sender": MEMBER},
            )
        )
    await async_session.commit()
    llm = ScriptedLLM([("text", "Hi")])
    _use(monkeypatch, llm)
    api = SimpleNamespace(post=AsyncMock())
    assert await emailbot.reply_once(api) is True
    assert llm.calls == []
    api.post.assert_not_awaited()
    assert await _state(async_session, email_id) == "skipped"


async def test_a_tool_call_reaches_the_email_run_route(
    async_session, monkeypatch
) -> None:
    email_id = await _seed(async_session)
    llm = ScriptedLLM(
        [
            (
                "tool",
                "send_email",
                '{"recipient_name": "Ada", "subject": "Hi", "body_text": "Hello"}',
            ),
            ("text", "Done."),
        ]
    )
    _use(monkeypatch, llm)
    api = SimpleNamespace(
        post=AsyncMock(
            return_value={"ok": True, "spoken": "Email sent.", "state": "done"}
        )
    )
    await emailbot.reply_once(api)
    path, payload = api.post.await_args_list[0].args
    assert path == "/internal/agent/email-run/send-email"
    assert payload["inbound_email_id"] == str(email_id)
    assert payload["recipient_name"] == "Ada"
    assert "call_id" not in payload
    assert api.post.await_args_list[1].args[0] == "/internal/agent/reply-email"
    assert llm.calls[1]["items"][-1].output == "Email sent."


async def test_an_unknown_tool_tells_the_model(async_session, monkeypatch) -> None:
    await _seed(async_session)
    llm = ScriptedLLM([("tool", "end_call", "{}"), ("text", "Sorry.")])
    _use(monkeypatch, llm)
    await emailbot.reply_once(
        SimpleNamespace(post=AsyncMock(return_value={"state": "done"}))
    )
    assert llm.calls[1]["items"][-1].output == emailbot.TOOL_FAILED


async def test_a_failing_tool_does_not_stop_the_reply(
    async_session, monkeypatch
) -> None:
    email_id = await _seed(async_session)
    llm = ScriptedLLM(
        [
            (
                "tool",
                "send_email",
                '{"recipient_name": "Ada", "subject": "Hi", "body_text": "Hello"}',
            ),
            ("text", "I could not send that."),
        ]
    )
    _use(monkeypatch, llm)

    async def post(path, payload):
        if "email-run" in path:
            raise RuntimeError("down")
        return {"state": "done"}

    await emailbot.reply_once(SimpleNamespace(post=post))
    assert llm.calls[1]["items"][-1].output == emailbot.TOOL_FAILED
    assert await _state(async_session, email_id) == "done"


async def test_tools_follow_the_agent_list(async_session) -> None:
    agent = Agent(
        organization_id=uuid.uuid4(),
        name="A",
        system_prompt="x",
        tools=["list_contacts"],
    )
    assert [s.name for s in await email_run_tools(agent, async_session)] == [
        "list_contacts"
    ]
    agent.tools = []
    assert await email_run_tools(agent, async_session) == []


async def test_call_only_tools_are_not_offered(async_session) -> None:
    agent = Agent(organization_id=uuid.uuid4(), name="A", system_prompt="x", tools=None)
    names = {s.name for s in await email_run_tools(agent, async_session)}
    assert not names & {"end_call", "send_dtmf", "transfer_call", "cancel_transfer"}


async def test_send_tools_refuse_without_a_call_or_an_email() -> None:
    api = SimpleNamespace(post=AsyncMock())
    ctx = ToolContext(
        call_id=None,
        organization_id=uuid.uuid4(),
        api=api,  # type: ignore[arg-type]
        hangup=None,
        send_dtmf=None,
    )
    args = {"recipient_name": "Ada", "subject": "s", "body_text": "b", "body": "b"}
    assert await send_email.SPEC.execute(ctx, args) == NO_CALL
    assert await send_sms.SPEC.execute(ctx, args) == NO_CALL
    assert await send_sms.EMAIL_RUN_SPEC.execute(ctx, args) != "ok"
    api.post.assert_not_awaited()
