"""Text worker: builds the chat from the thread, posts the reply, records the state."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core.models import Agent, PhoneNumber, Sms
from hailhq.core.sms_ingest import ingest_inbound_sms
from hailhq.voicebot import textbot
from hailhq.voicebot.pipeline import ProviderKeyError

from ._fakes import FakeLLM

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


async def _seed(session):
    org = uuid.uuid4()
    agent = Agent(
        organization_id=org, name="Front desk", system_prompt="Book appointments."
    )
    session.add(agent)
    await session.flush()
    session.add(
        PhoneNumber(
            organization_id=org,
            e164=ORG_NUMBER,
            country_code="US",
            number_type="local",
            provisioning_state="active",
            provider_resource_id="PN",
            sms_agent_id=agent.id,
        )
    )
    await session.commit()
    result = await ingest_inbound_sms(
        session,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        body="Can I book for Tuesday?",
        provider_message_sid="SM1",
        opt_out_type=None,
    )
    await session.commit()
    return result.sms_id


@pytest.fixture()
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> dict:
    seen: dict = {}

    async def fake_resolve(_org, *, skip_llm=False):
        return {}

    def fake_build(llm_cfg, org=None):
        return FakeLLM(reply="Yes, Tuesday at 10:00 works. Shall I book it?")

    monkeypatch.setattr(textbot, "resolve_org_configs", fake_resolve)
    monkeypatch.setattr(textbot, "build_llm", fake_build)
    return seen


async def test_reply_once_posts_the_model_text(async_session, fake_llm) -> None:
    sms_id = await _seed(async_session)
    api = SimpleNamespace(post=AsyncMock(return_value={"ok": True, "state": "done"}))

    assert await textbot.reply_once(api) is True

    api.post.assert_awaited_once_with(
        "/internal/agent/reply-sms",
        {
            "sms_id": str(sms_id),
            "body": "Yes, Tuesday at 10:00 works. Shall I book it?",
        },
    )
    async_session.expire_all()
    row = await async_session.get(Sms, sms_id)
    assert row.agent_reply_state == "done"
    # Nothing left to do.
    assert await textbot.reply_once(api) is False


async def test_skipped_and_failed_states_are_recorded(async_session, fake_llm) -> None:
    sms_id = await _seed(async_session)
    api = SimpleNamespace(
        post=AsyncMock(
            return_value={"ok": False, "state": "skipped", "reason": "thread_cap"}
        )
    )
    await textbot.reply_once(api)
    async_session.expire_all()
    assert (await async_session.get(Sms, sms_id)).agent_reply_state == "skipped"


async def test_provider_error_marks_failed_without_posting(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sms_id = await _seed(async_session)

    async def boom(_org, *, skip_llm=False):
        raise ProviderKeyError("no key")

    monkeypatch.setattr(textbot, "resolve_org_configs", boom)
    api = SimpleNamespace(post=AsyncMock())
    await textbot.reply_once(api)
    api.post.assert_not_awaited()
    async_session.expire_all()
    assert (await async_session.get(Sms, sms_id)).agent_reply_state == "failed"


async def test_generate_reply_builds_chat_from_thread(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}

    class _RecordingLLM(FakeLLM):
        def chat(self, *, chat_ctx, **kwargs):
            captured["roles"] = [m.role for m in chat_ctx.items]
            return super().chat(chat_ctx=chat_ctx, **kwargs)

    async def fake_resolve(_org, *, skip_llm=False):
        return {}

    monkeypatch.setattr(textbot, "resolve_org_configs", fake_resolve)
    monkeypatch.setattr(
        textbot, "build_llm", lambda cfg, org=None: _RecordingLLM(reply="ok " * 400)
    )
    claimed = SimpleNamespace(agent=SimpleNamespace(organization_id=uuid.uuid4()))
    text = await textbot.generate_reply(
        claimed,  # type: ignore[arg-type]
        [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": "hi"},
        ],
    )
    assert captured["roles"] == ["system", "assistant", "user"]
    assert len(text) <= textbot.MAX_REPLY_CHARS
