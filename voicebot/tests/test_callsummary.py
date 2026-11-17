"""Call analysis worker: summary, fallback to null, and no model when unneeded."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from hailhq.core import call_analysis
from hailhq.core.models import (
    Agent,
    Call,
    CallEvent,
    PhoneNumber,
    WebhookDelivery,
    WebhookSubscription,
)
from hailhq.core.prompts import CALL_SUMMARY_INSTRUCTIONS
from hailhq.voicebot import callsummary
from hailhq.voicebot.pipeline import ProviderKeyError
from sqlalchemy import select

from ._fakes import ScriptedLLM

ORG_NUMBER = "+14155550100"
PERSON = "+33612345678"


async def _seed(session, *, subscribe=True, turns=None) -> uuid.UUID:
    org = uuid.uuid4()
    agent = Agent(organization_id=org, name="Front desk", system_prompt="Help.")
    session.add(agent)
    await session.flush()
    number = PhoneNumber(
        organization_id=org,
        e164=ORG_NUMBER,
        country_code="US",
        number_type="local",
        provisioning_state="active",
        provider_resource_id="PN",
    )
    session.add(number)
    await session.flush()
    ended_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    call = Call(
        organization_id=org,
        agent_id=agent.id,
        to_number_id=number.id,
        from_e164=PERSON,
        to_e164=ORG_NUMBER,
        direction="inbound",
        status="completed",
        end_reason="normal_hangup",
        provider="twilio",
        voice_config={},
        answered_at=ended_at - timedelta(seconds=30),
        ended_at=ended_at,
    )
    session.add(call)
    await session.flush()
    for i, (kind, text) in enumerate(
        turns
        if turns is not None
        else [("agent_turn", "Hi, this is Acme."), ("user_turn", "Move my booking.")]
    ):
        session.add(
            CallEvent(
                call_id=call.id,
                kind=kind,
                payload={"text": text},
                occurred_at=ended_at - timedelta(seconds=20 - i),
            )
        )
    if subscribe:
        session.add(
            WebhookSubscription(
                organization_id=org,
                target_url="https://example.com/hook",
                event_types=["call.analyzed"],
                secret_encrypted="x",
                created_at=ended_at - timedelta(hours=1),
                updated_at=ended_at - timedelta(hours=1),
            )
        )
    await session.commit()
    return call.id


async def _delivery(session, call_id) -> WebhookDelivery | None:
    return (
        await session.execute(
            select(WebhookDelivery).where(
                WebhookDelivery.event_type == "call.analyzed",
                WebhookDelivery.event_id == call_id,
            )
        )
    ).scalar_one_or_none()


@pytest.fixture(autouse=True)
def _clear_failures():
    callsummary._failures.clear()
    yield
    callsummary._failures.clear()


@pytest.fixture()
def llm(monkeypatch: pytest.MonkeyPatch) -> dict:
    state: dict = {"built": 0, "llm": ScriptedLLM([("text", "  Booking moved.  ")])}

    async def fake_resolve(_org, *, skip_llm=False):
        return {}

    def fake_build(llm_cfg, org=None):
        state["built"] += 1
        if isinstance(state["llm"], Exception):
            raise state["llm"]
        return state["llm"]

    monkeypatch.setattr(callsummary, "resolve_org_configs", fake_resolve)
    monkeypatch.setattr(callsummary, "build_llm", fake_build)
    return state


async def test_sends_summary_and_transcript(async_session, llm) -> None:
    call_id = await _seed(async_session)
    assert await callsummary.analyze_once()
    delivery = await _delivery(async_session, call_id)
    assert delivery is not None
    data = delivery.payload["data"]
    assert data["summary"] == "Booking moved."
    assert [t["role"] for t in data["transcript"]] == ["assistant", "user"]
    assert data["duration_seconds"] == 30
    # The model sees the prompt and the transcript, and gets no tools.
    [seen] = llm["llm"].calls
    texts = [item.text_content for item in seen["items"]]
    assert texts == [
        CALL_SUMMARY_INSTRUCTIONS,
        "Agent: Hi, this is Acme.\nPerson: Move my booking.",
    ]
    assert seen["tools"] == []
    assert llm["llm"].aclosed
    # Nothing left to do.
    assert not await callsummary.analyze_once()


@pytest.mark.parametrize(
    "failure", [RuntimeError("model down"), ProviderKeyError("no key")]
)
async def test_failed_summary_still_sends(async_session, llm, caplog, failure) -> None:
    llm["llm"] = failure
    call_id = await _seed(async_session)
    with caplog.at_level(logging.WARNING, logger="hailhq.voicebot.callsummary"):
        assert await callsummary.analyze_once()
    delivery = await _delivery(async_session, call_id)
    assert delivery is not None
    assert delivery.payload["data"]["summary"] is None
    assert "call summary failed" in caplog.text


async def test_empty_reply_is_null_summary(async_session, llm) -> None:
    llm["llm"] = ScriptedLLM([("text", "   ")])
    call_id = await _seed(async_session)
    assert await callsummary.analyze_once()
    assert (await _delivery(async_session, call_id)).payload["data"]["summary"] is None


async def test_no_subscription_never_calls_the_model(async_session, llm) -> None:
    await _seed(async_session, subscribe=False)
    assert not await callsummary.analyze_once()
    assert llm["built"] == 0


async def test_blank_transcript_is_marked_without_a_model_call(
    async_session, llm
) -> None:
    call_id = await _seed(async_session, turns=[("user_turn", "  ")])
    assert await callsummary.analyze_once()
    assert llm["built"] == 0
    assert await _delivery(async_session, call_id) is None
    assert not await callsummary.analyze_once()


async def test_two_workers_run_the_model_once(async_session, llm, monkeypatch) -> None:
    call_id = await _seed(async_session)
    real = callsummary.summarize

    async def slow_summarize(*args):
        # Both workers poll while the first one waits on the model.
        await asyncio.sleep(0.3)
        return await real(*args)

    monkeypatch.setattr(callsummary, "summarize", slow_summarize)
    results = await asyncio.gather(
        callsummary.analyze_once(), callsummary.analyze_once()
    )
    assert sorted(results) == [False, True]
    assert llm["built"] == 1
    assert (await _delivery(async_session, call_id)) is not None


async def test_failed_send_is_retried_then_given_up(
    async_session, llm, monkeypatch
) -> None:
    call_id = await _seed(async_session)
    # A failed try keeps its claim until the lease ends; end it at once.
    monkeypatch.setattr(call_analysis, "CLAIM_LEASE", timedelta(0))

    sent_turns: list = []

    async def broken_send(db, cid, turns, summary):
        sent_turns.append(turns)
        raise RuntimeError("db down")

    monkeypatch.setattr(callsummary, "send_call_analyzed", broken_send)
    for _ in range(callsummary.MAX_SEND_ATTEMPTS):
        with pytest.raises(RuntimeError):
            await callsummary.analyze_once()
        # A turn row written late does not change what a retry sends: the
        # summary was written from the first transcript.
        async_session.add(
            CallEvent(call_id=call_id, kind="user_turn", payload={"text": "Late."})
        )
        await async_session.commit()
    assert all(t == sent_turns[0] for t in sent_turns)
    assert len(sent_turns[0]) == 2
    assert callsummary._failures[call_id].count == callsummary.MAX_SEND_ATTEMPTS
    # The model ran once: retries reuse its summary.
    assert llm["built"] == 1
    # Given up in this process: the call is no longer picked.
    assert not await callsummary.analyze_once()


async def test_failure_before_the_send_counts_too(
    async_session, llm, monkeypatch
) -> None:
    call_id = await _seed(async_session)
    monkeypatch.setattr(call_analysis, "CLAIM_LEASE", timedelta(0))

    async def broken_transcript(db, cid):
        raise RuntimeError("bad row")

    monkeypatch.setattr(callsummary, "call_transcript", broken_transcript)
    for _ in range(callsummary.MAX_SEND_ATTEMPTS):
        with pytest.raises(RuntimeError):
            await callsummary.analyze_once()
    assert call_id in callsummary._given_up()
    assert not await callsummary.analyze_once()


def test_long_transcript_keeps_start_and_end() -> None:
    turns = [{"role": "user", "text": "x" * 1000} for _ in range(200)]
    turns[0]["text"] = "START"
    turns[-1]["text"] = "END"
    text = callsummary._transcript_text(turns)
    assert len(text) <= callsummary.MAX_PROMPT_CHARS + 10
    assert text.startswith("Person: START") and text.endswith("Person: END")
