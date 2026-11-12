from types import SimpleNamespace

import pytest
from hailhq.voicebot.browser import browser_instructions, deadline_seconds


def test_browser_runtime_scopes_real_sends_and_warns_about_unreadable_files():
    result = browser_instructions("Collect a callback request.")
    assert "Collect a callback request." in result
    assert "browser" in result
    assert "verified account email" in result
    assert "three emails and three texts" in result
    assert "inspect images, PDFs" in result
    assert "stored original" in result
    assert "telephone call" not in result


def test_server_deadline_uses_persisted_budget_not_dispatch_input():
    assert deadline_seconds(SimpleNamespace(max_duration_seconds=60)) == 60
    assert deadline_seconds(SimpleNamespace(max_duration_seconds=600)) == 600
    assert deadline_seconds(SimpleNamespace(max_duration_seconds=99999)) == 600


@pytest.mark.asyncio
@pytest.mark.parametrize("delete_fails", [False, True])
@pytest.mark.parametrize("shutdown_during_disconnect", [False, True])
@pytest.mark.parametrize("readiness_delay", [0, 2])
async def test_hard_deadline_closes_room_and_finalizes_trial_without_client_timer(
    async_session,
    monkeypatch,
    delete_fails,
    shutdown_during_disconnect,
    readiness_delay,
):
    import asyncio
    import uuid
    from unittest.mock import AsyncMock

    from hailhq.core.models import Call
    from hailhq.voicebot import browser

    call = Call(
        organization_id=uuid.uuid4(),
        transport="browser",
        direction="inbound",
        provider="browser",
        voice_config={},
        max_duration_seconds=1,
        metadata_={"browser_trial": True},
        status="queued",
    )
    async_session.add(call)
    await async_session.commit()
    closed = asyncio.Event()

    class Session:
        history = SimpleNamespace(to_dict=lambda: {"items": []})

        def on(self, event):
            return lambda callback: callback

        async def start(self, **kwargs):
            assert kwargs["record"] is False

        async def aclose(self):
            pass

        def generate_reply(self, **kwargs):
            pass

    class Room:
        disconnect = AsyncMock()
        local_participant = SimpleNamespace(
            set_attributes=AsyncMock(), publish_data=AsyncMock()
        )

        def on(self, event):
            return lambda callback: callback

    class Context:
        room = Room()
        proc = SimpleNamespace(userdata={"vad": None})

        def add_shutdown_callback(self, fn):
            self.callback = fn

        async def connect(self):
            pass

        async def wait_for_participant(self):
            return SimpleNamespace(identity="browser-user")

        async def delete_room(self):
            if delete_fails:
                raise TimeoutError("room API unavailable")
            closed.set()

        def shutdown(self, **kwargs):
            closed.set()

    if readiness_delay:
        from contextlib import asynccontextmanager

        original_scope = browser.session_scope

        @asynccontextmanager
        async def slow_readiness_scope():
            async with original_scope() as db:
                original_get = db.get

                async def get(*args, **kwargs):
                    if kwargs.get("with_for_update"):
                        await asyncio.sleep(readiness_delay)
                    return await original_get(*args, **kwargs)

                db.get = get
                yield db

        monkeypatch.setattr(browser, "session_scope", slow_readiness_scope)
    monkeypatch.setattr(browser, "resolve_org_configs", AsyncMock(return_value={}))
    monkeypatch.setattr(browser, "build_session", lambda *a, **kw: Session())
    ctx = Context()
    if shutdown_during_disconnect:

        async def disconnected():
            asyncio.create_task(ctx.callback())

        ctx.room.disconnect.side_effect = disconnected
    await browser.run_browser_session(ctx, call.id)
    await asyncio.wait_for(closed.wait(), timeout=3)
    await asyncio.sleep(0.1)
    await async_session.refresh(call)
    ctx.room.disconnect.assert_awaited()
    assert call.status == "completed"
    assert call.metadata_["browser_used_ms"] == 1000
    await ctx.callback()


def test_transcript_does_not_expose_system_instructions_or_tool_results():
    from hailhq.voicebot.browser import public_transcript

    result = public_transcript(
        {
            "items": [
                {"role": "system", "content": ["private runtime"]},
                {"role": "user", "content": ["Hello", {"audio": "bytes"}]},
                {"role": "tool", "content": ["internal"]},
            ]
        }
    )
    assert result == {"items": [{"role": "user", "content": ["Hello"]}]}


def test_browser_uses_saved_workspace_language_for_speech_and_llm():
    from hailhq.voicebot.browser import browser_language

    providers = {"tts": SimpleNamespace(params={"language": "sv"})}
    assert browser_language({}, providers) == "sv"
    assert browser_language({"language": "en"}, providers) == "en"
    assert "Speak in Swedish" in browser_instructions(
        "Collect a callback request.", "sv"
    )
    assert browser_language({"language": "not-a-language"}, {}) is None


def test_browser_tools_hide_unavailable_channels_and_never_expose_phone_tools(
    monkeypatch,
):
    import uuid

    from hailhq.core.config import settings
    from hailhq.voicebot.browser_tools import build_browser_tools

    monkeypatch.setattr(settings, "hail_internal_secret", "test")
    tools, _api = build_browser_tools(uuid.uuid4(), uuid.uuid4(), {})
    assert tools == []
    tools, _api = build_browser_tools(
        uuid.uuid4(),
        uuid.uuid4(),
        {"emailSendReady": True, "smsSendReady": True, "inboundEmailReady": True},
    )
    assert {tool.info.name for tool in tools} == {
        "send_email",
        "send_sms",
        "read_browser_inbox",
    }


def test_browser_can_read_texts_only_when_assigned_a_number(monkeypatch):
    import uuid

    from hailhq.core.config import settings
    from hailhq.voicebot.browser_tools import build_browser_tools

    monkeypatch.setattr(settings, "hail_internal_secret", "test")
    tools, _api = build_browser_tools(
        uuid.uuid4(), uuid.uuid4(), {"inboundNumber": "+14155550111"}
    )
    assert {tool.info.name for tool in tools} == {"read_browser_texts"}


@pytest.mark.asyncio
async def test_browser_receipts_report_acceptance_and_never_delivery(monkeypatch):
    import json
    from unittest.mock import AsyncMock

    from hailhq.core.agent_tools.client import AgentApiClient
    from hailhq.voicebot.browser_tools import BrowserApiClient

    post = AsyncMock(return_value={"ok": True, "spoken": "Email sent."})
    monkeypatch.setattr(AgentApiClient, "post", post)
    publish = AsyncMock()
    api = BrowserApiClient({"emailRecipient": "owner@example.test"}, publish)
    await api.post(
        "/internal/agent/send-email",
        {"subject": "Test", "body_text": "Hello", "tool_invocation_id": "request-id"},
    )
    event = json.loads(publish.await_args.args[0])
    assert event == {
        "channel": "email",
        "recipient": "owner@example.test",
        "subject": "Test",
        "body": "Hello",
        "status": "accepted",
        "id": "request-id",
    }
    assert publish.await_args.kwargs["topic"] == "hail.message"
    post.return_value = {
        "ok": True,
        "spoken": "The email is still being submitted. Do not send it again.",
    }
    await api.post("/internal/agent/send-email", {})
    assert publish.await_count == 1
    post.return_value = {"ok": False, "spoken": "I could not send it."}
    await api.post("/internal/agent/send-email", {})
    assert json.loads(publish.await_args.args[0])["status"] == "failed"


@pytest.mark.asyncio
async def test_browser_agent_refreshes_mail_before_an_order_question_without_mail_keywords():
    from unittest.mock import AsyncMock

    from hailhq.voicebot.browser import BrowserAgent
    from livekit.agents import llm

    api = SimpleNamespace(
        post=AsyncMock(
            side_effect=[
                {
                    "quoted_untrusted_messages": [
                        {
                            "id": "mail-1",
                            "subject": "Order receipt",
                            "body_text": "Order number: EXAMPLE-4821",
                            "attachments": [],
                        }
                    ]
                },
                {
                    "quoted_untrusted_messages": [
                        {
                            "id": "mail-2",
                            "subject": "Update",
                            "body_text": "Order number: EXAMPLE-4822",
                            "attachments": [],
                        }
                    ]
                },
            ]
        )
    )
    agent = BrowserAgent(
        call_id="call-1",
        messaging_api=api,
        capabilities={"inboundEmailReady": True},
        instructions="Help with orders.",
    )
    for expected in ("EXAMPLE-4821", "EXAMPLE-4822"):
        ctx = llm.ChatContext()
        await agent.on_user_turn_completed(
            ctx, llm.ChatMessage(role="user", content=["What is my order number?"])
        )
        assert expected in ctx.items[-1].text_content
        assert "untrusted" in ctx.items[-1].text_content
        assert ctx.items[-1].role == "system"
        assert __import__(
            "hailhq.voicebot.browser", fromlist=["public_transcript"]
        ).public_transcript(ctx.to_dict()) == {"items": []}
    assert api.post.await_count == 2


@pytest.mark.asyncio
async def test_browser_agent_does_not_confuse_inbox_failure_with_empty_mail():
    from unittest.mock import AsyncMock

    from hailhq.voicebot.browser import BrowserAgent
    from livekit.agents import llm

    api = SimpleNamespace(post=AsyncMock(side_effect=RuntimeError("offline")))
    agent = BrowserAgent(
        call_id="call-1",
        messaging_api=api,
        capabilities={"inboundEmailReady": True},
        instructions="Help.",
    )
    ctx = llm.ChatContext()
    await agent.on_user_turn_completed(
        ctx, llm.ChatMessage(role="user", content=["Hello"])
    )
    assert "could not be checked" in ctx.items[-1].text_content
    assert "Do not claim no messages arrived" in ctx.items[-1].text_content


def test_browser_codes_are_spoken_as_characters_without_rewriting_message_values():
    result = browser_instructions("Help with orders.")
    assert "character by character" in result
    assert "one, two, two; one, three, three, four" in result
    assert "X as in X-ray; Y as in Yankee" in result
    assert "Never change the original code in written messages" in result


def test_browser_module_parses_on_supported_python_311():
    import ast
    from pathlib import Path

    from hailhq.voicebot import browser

    ast.parse(Path(browser.__file__).read_text(), feature_version=(3, 11))
