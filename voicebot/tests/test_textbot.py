"""Text worker: builds the chat from the thread, posts the reply, records the state."""

from __future__ import annotations

import asyncio
import threading
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hailhq.core import text_agent
from hailhq.core.models import Agent, PhoneNumber, Sms
from hailhq.core.sms_ingest import ingest_inbound_sms
from hailhq.voicebot import textbot
from hailhq.voicebot.pipeline import ProviderKeyError
from sqlalchemy import select

from ._fakes import FakeLLM, ScriptedLLM

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
        carrier="twilio",
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
    text = await textbot.generate_reply(
        _claimed(),  # type: ignore[arg-type]
        [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "earlier"},
            {"role": "user", "content": "hi"},
        ],
    )
    assert captured["roles"] == ["system", "assistant", "user"]
    assert len(text) <= textbot.MAX_REPLY_CHARS


def _claimed(org=None, agent_id=None):
    return SimpleNamespace(
        agent=SimpleNamespace(organization_id=org or uuid.uuid4(), id=agent_id),
        sms=SimpleNamespace(id=uuid.uuid4(), from_e164=PERSON, to_e164=ORG_NUMBER),
    )


_MESSAGES = [
    {"role": "system", "content": "sys"},
    {"role": "user", "content": "what was my order number?"},
]


def _use(monkeypatch, llm) -> None:
    async def fake_resolve(_org, *, skip_llm=False):
        return {}

    monkeypatch.setattr(textbot, "resolve_org_configs", fake_resolve)
    monkeypatch.setattr(textbot, "build_llm", lambda cfg, org=None: llm)


async def test_chat_from_the_db_has_only_texts_and_the_current_text_last(
    async_session,
) -> None:
    from hailhq.core.models import Call, CallEvent

    sms_id = await _seed(async_session)
    sms = await async_session.get(Sms, sms_id)
    call = Call(
        organization_id=sms.organization_id,
        agent_id=sms.agent_id,
        to_number_id=sms.to_number_id,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status="completed",
        end_reason="normal_hangup",
        provider="twilio",
        voice_config={},
    )
    async_session.add(call)
    await async_session.flush()
    async_session.add(
        CallEvent(
            call_id=call.id,
            kind="agent_turn",
            payload={"role": "assistant", "text": "I don't have that number."},
            occurred_at=datetime.now(timezone.utc),
        )
    )
    await async_session.commit()
    claimed = await text_agent.claim_pending_reply(async_session)

    messages = await textbot._prepare(claimed)

    assert [m["role"] for m in messages] == ["system", "user"]
    assert messages[-1]["content"] == "Can I book for Tuesday?"
    assert "I don't have that number." not in str(messages)


async def test_tool_loop_reads_the_thread_then_answers(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sms_id = await _seed(async_session)
    sms = await async_session.get(Sms, sms_id)
    async_session.add(
        Sms(
            organization_id=sms.organization_id,
            agent_id=sms.agent_id,
            provider="twilio",
            from_e164=PERSON,
            to_e164=ORG_NUMBER,
            direction="inbound",
            status="received",
            body="order 4411",
            requested_at=datetime.now(timezone.utc) - timedelta(days=3),
        )
    )
    await async_session.commit()
    llm = ScriptedLLM(
        [
            ("tool", "thread_history", '{"source": "sms"}'),
            ("text", "Your order is 4411."),
        ]
    )
    _use(monkeypatch, llm)

    text = await textbot.generate_reply(
        _claimed(sms.organization_id, sms.agent_id), _MESSAGES  # type: ignore[arg-type]
    )

    assert text == "Your order is 4411."
    assert len(llm.calls) == 2
    assert [t.info.name for t in llm.calls[0]["tools"]] == ["thread_history"]
    second = llm.calls[1]["items"]
    assert [i.type for i in second[-2:]] == ["function_call", "function_call_output"]
    assert "order 4411" in second[-1].output
    assert second[-1].call_id == second[-2].call_id
    assert llm.aclosed


async def test_tool_scope_comes_from_the_text_not_the_model(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sms_id = await _seed(async_session)
    sms = await async_session.get(Sms, sms_id)
    async_session.add(
        Sms(
            organization_id=sms.organization_id,
            agent_id=sms.agent_id,
            provider="twilio",
            from_e164="+33699999999",
            to_e164=ORG_NUMBER,
            direction="inbound",
            status="received",
            body="someone else's secret",
        )
    )
    await async_session.commit()
    llm = ScriptedLLM(
        [
            ("tool", "thread_history", '{"caller": "+33699999999"}'),
            ("text", "ok"),
        ]
    )
    _use(monkeypatch, llm)

    await textbot.generate_reply(
        _claimed(sms.organization_id, sms.agent_id), _MESSAGES  # type: ignore[arg-type]
    )

    output = llm.calls[1]["items"][-1].output
    assert "Can I book for Tuesday?" in output
    assert "secret" not in output


async def test_tool_loop_stops_after_three_rounds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_execute(ctx, args):
        return "nothing"

    monkeypatch.setattr(textbot, "THREAD_HISTORY", _spec_with(fake_execute))
    llm = ScriptedLLM(
        [
            ("tool", "thread_history", "{}"),
            ("tool", "thread_history", "{}"),
            ("tool", "thread_history", "{}"),
            ("text", "never asked"),
        ],
        text_with_tool="Let me check.",
    )
    _use(monkeypatch, llm)

    text = await textbot.generate_reply(_claimed(), _MESSAGES)  # type: ignore[arg-type]

    assert len(llm.calls) == textbot.MAX_TOOL_ROUNDS == 3
    assert llm.calls[-1]["tool_choice"] == "none"
    assert text == "Let me check."


async def test_tool_call_without_text_on_the_last_round_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_execute(ctx, args):
        return "nothing"

    monkeypatch.setattr(textbot, "THREAD_HISTORY", _spec_with(fake_execute))
    llm = ScriptedLLM([("tool", "thread_history", "{}")] * 3)
    _use(monkeypatch, llm)

    with pytest.raises(RuntimeError):
        await textbot.generate_reply(_claimed(), _MESSAGES)  # type: ignore[arg-type]


async def test_tool_error_becomes_an_apology_not_an_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(ctx, args):
        raise RuntimeError("db down")

    monkeypatch.setattr(textbot, "THREAD_HISTORY", _spec_with(boom))
    llm = ScriptedLLM(
        [("tool", "thread_history", "not json"), ("text", "Sorry, try again later.")]
    )
    _use(monkeypatch, llm)

    text = await textbot.generate_reply(_claimed(), _MESSAGES)  # type: ignore[arg-type]

    assert text == "Sorry, try again later."
    out = llm.calls[1]["items"][-1]
    assert out.output == textbot.TOOL_APOLOGY
    assert out.is_error is True


async def test_unknown_tool_gets_a_plain_refusal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = ScriptedLLM([("tool", "send_sms", "{}"), ("text", "ok")])
    _use(monkeypatch, llm)

    await textbot.generate_reply(_claimed(), _MESSAGES)  # type: ignore[arg-type]

    assert llm.calls[1]["items"][-1].output == textbot.TOOL_APOLOGY


async def test_reply_after_a_tool_is_trimmed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_execute(ctx, args):
        return "nothing"

    monkeypatch.setattr(textbot, "THREAD_HISTORY", _spec_with(fake_execute))
    llm = ScriptedLLM([("tool", "thread_history", "{}"), ("text", "x" * 2000)])
    _use(monkeypatch, llm)

    text = await textbot.generate_reply(_claimed(), _MESSAGES)  # type: ignore[arg-type]

    assert text == "x" * textbot.MAX_REPLY_CHARS


def _spec_with(execute):
    import dataclasses

    return dataclasses.replace(textbot.THREAD_HISTORY, execute=execute)


async def test_transient_post_error_leaves_the_text_retryable(
    async_session, fake_llm
) -> None:
    sms_id = await _seed(async_session)
    api = SimpleNamespace(post=AsyncMock(side_effect=RuntimeError("api down")))
    assert await textbot.reply_once(api) is True
    async_session.expire_all()
    row = await async_session.get(Sms, sms_id)
    assert row.agent_reply_state == "pending"
    assert row.agent_reply_attempts == 1
    assert row.agent_reply_available_at > datetime.now(timezone.utc)
    # Backoff: not claimed again yet.
    assert await textbot.reply_once(api) is False

    # Backoff over: the retry succeeds.
    row.agent_reply_available_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await async_session.commit()
    api.post = AsyncMock(return_value={"ok": True, "state": "done"})
    assert await textbot.reply_once(api) is True
    async_session.expire_all()
    row = await async_session.get(Sms, sms_id)
    assert row.agent_reply_state == "done"
    assert row.agent_reply_attempts == 2


async def test_transient_errors_fail_after_the_attempt_limit(
    async_session, fake_llm
) -> None:
    sms_id = await _seed(async_session)
    api = SimpleNamespace(post=AsyncMock(side_effect=RuntimeError("api down")))
    for _ in range(text_agent.MAX_ATTEMPTS):
        async_session.expire_all()
        row = await async_session.get(Sms, sms_id)
        row.agent_reply_available_at = None
        await async_session.commit()
        assert await textbot.reply_once(api) is True
    async_session.expire_all()
    row = await async_session.get(Sms, sms_id)
    assert row.agent_reply_state == "failed"
    assert api.post.await_count == text_agent.MAX_ATTEMPTS


async def test_model_error_is_retried_not_failed(
    async_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    sms_id = await _seed(async_session)

    async def boom(_org, *, skip_llm=False):
        raise RuntimeError("model down")

    monkeypatch.setattr(textbot, "resolve_org_configs", boom)
    api = SimpleNamespace(post=AsyncMock())
    await textbot.reply_once(api)
    api.post.assert_not_awaited()
    async_session.expire_all()
    assert (await async_session.get(Sms, sms_id)).agent_reply_state == "pending"


async def test_no_row_lock_is_held_while_the_model_runs(
    async_session, session_factory, monkeypatch: pytest.MonkeyPatch
) -> None:
    sms_id = await _seed(async_session)
    seen: dict = {}

    async def fake_generate(claimed, messages):
        async with session_factory() as other:
            await other.execute(
                select(Sms).where(Sms.id == sms_id).with_for_update(nowait=True)
            )
            seen["state"] = (await other.get(Sms, sms_id)).agent_reply_state
        return "ok"

    monkeypatch.setattr(textbot, "generate_reply", fake_generate)
    api = SimpleNamespace(post=AsyncMock(return_value={"ok": True, "state": "done"}))
    await textbot.reply_once(api)
    assert seen["state"] == "processing"


async def test_run_forever_answers_several_texts_at_once(
    async_session, fake_llm, monkeypatch: pytest.MonkeyPatch
) -> None:
    from hailhq.core.config import settings

    monkeypatch.setattr(settings, "hail_text_reply_concurrency", 3)
    monkeypatch.setattr(settings, "hail_api_url", "http://api")
    monkeypatch.setattr(settings, "hail_internal_secret", "s")
    await _seed(async_session)  # one text
    for i in (1, 2):
        await ingest_inbound_sms(
            async_session,
            from_e164=f"+3361234567{i}",
            to_e164=ORG_NUMBER,
            body="hi",
            provider_message_sid=f"SMX{i}",
            opt_out_type=None,
            carrier="twilio",
        )
    await async_session.commit()
    inflight = 0
    peak = 0

    async def slow_post(path, payload):
        nonlocal inflight, peak
        inflight += 1
        peak = max(peak, inflight)
        await asyncio.sleep(0.3)
        inflight -= 1
        return {"ok": True, "state": "done"}

    stop = threading.Event()
    monkeypatch.setattr(
        textbot,
        "AgentApiClient",
        lambda *a, **k: SimpleNamespace(post=slow_post, aclose=AsyncMock()),
    )
    task = asyncio.create_task(textbot.run_forever(stop))
    await asyncio.sleep(1.5)
    stop.set()
    await asyncio.wait_for(task, 10)
    assert peak >= 2
