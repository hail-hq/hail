import asyncio
import uuid
from unittest.mock import AsyncMock

from hailhq.core.agent_tools import transfer_call
from hailhq.core.agent_tools.spec import BridgeOutcome, ToolContext

CID = str(uuid.uuid4())
META = {"handover_targets": [{"contact_id": CID, "label": "Sam", "note": "Billing"}]}


RESULT_PATH = "/internal/agent/handover-result"
SMS_PATH = "/internal/agent/handover-sms"
EMAIL_PATH = "/internal/agent/handover-email"


def _post_by_path(route_reply, result_replies=None, sms_reply=None, email_reply=None):
    """``api.post`` by path: the route reply, the text and email replies, then
    ``result_replies`` in order for the result posts (then ``{"ok": True}``)."""
    results = list(result_replies or [])

    async def post(path, body):
        if path == "/internal/agent/handover":
            return route_reply
        reply = {SMS_PATH: sms_reply, EMAIL_PATH: email_reply}.get(path)
        if path == RESULT_PATH:
            reply = results.pop(0) if results else None
        if isinstance(reply, Exception):
            raise reply
        if reply is not None:
            return reply
        return {"ok": True, "sms": "sent", "email": "sent"}

    return post


def _posted(api, path):
    return [c.args[1] for c in api.post.await_args_list if c.args[0] == path]


async def _settled() -> None:
    """Wait for the background dial, text and email to finish."""
    while transfer_call._pending:
        await asyncio.gather(*list(transfer_call._pending))


def _ctx(api_reply, outcome=None):
    api = AsyncMock()
    api.post.side_effect = _post_by_path(api_reply)
    bridge = AsyncMock(return_value=outcome)
    return (
        ToolContext(
            call_id=uuid.uuid4(),
            organization_id=uuid.uuid4(),
            api=api,
            hangup=None,
            send_dtmf=None,
            bridge=bridge,
            prompt=AsyncMock(),
        ),
        api,
        bridge,
    )


ROUTE = {
    "ok": True,
    "spoken": "",
    "to_e164": "+14155550120",
    "from_e164": "+14155550100",
    "trunk_id": "ST_x",
    "headers": None,
}


def test_bind_hides_without_targets() -> None:
    assert transfer_call.bind({"handover_targets": []}) is None
    assert transfer_call.bind({}) is None


def test_bind_lists_names_without_numbers() -> None:
    spec = transfer_call.bind(META)
    assert spec is not None
    assert "Sam" in spec.description and "Billing" in spec.description
    assert spec.parameters["properties"]["contact"]["enum"] == ["Sam"]
    assert spec.risk_tier == "session_control"


def test_description_tells_the_caller_they_can_stop() -> None:
    spec = transfer_call.bind(META)
    assert "still here while it rings" in spec.description
    assert "say stop" in spec.description


async def test_dial_runs_in_the_background() -> None:
    """The tool returns at once with no reply turn; the dial goes on while
    the agent keeps talking with the caller."""
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    ring = asyncio.Event()

    async def bridge(_route):
        await ring.wait()
        return BridgeOutcome("no_answer", 480, 30000)

    ctx.bridge = bridge
    assert await spec.execute(ctx, {"contact": "Sam"}) is None
    assert _posted(api, RESULT_PATH) == []
    ring.set()
    await _settled()
    assert _posted(api, RESULT_PATH)[0]["outcome"] == "no_answer"


async def test_answered() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE, BridgeOutcome("answered", None, 9000))
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "an invoice"})
    assert said is None
    await _settled()
    route = bridge.await_args.args[0]
    assert route.to_e164 == "+14155550120" and route.name == "Sam"
    assert api.post.await_args_list[0].args == (
        "/internal/agent/handover",
        {"call_id": str(ctx.call_id), "contact_id": CID},
    )
    result = _posted(api, RESULT_PATH)[0]
    assert result["outcome"] == "answered" and result["ring_ms"] == 9000
    ctx.prompt.assert_not_awaited()  # the agent is muted: the two people talk


async def test_person_on_the_call_is_listed_but_never_dialed() -> None:
    """The caller is calling from the handover person's own phone: the agent
    is told so up front, and a call to the tool explains instead of
    dialing."""
    meta = {
        "handover_targets": [
            {"contact_id": CID, "label": "Sam", "note": "Billing", "on_call": True}
        ]
    }
    spec = transfer_call.bind(meta)
    line = (
        "It seems this call is already on Sam's number. Handing it over to the "
        "same number is not technically possible."
    )
    assert "already on this call" in spec.description and line in spec.description
    ctx, api, bridge = _ctx(ROUTE)
    assert await spec.execute(ctx, {"contact": "Sam", "reason": "x"}) == line
    api.post.assert_not_awaited()
    bridge.assert_not_awaited()


async def test_no_answer_has_the_agent_tell_the_caller() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    ctx.prompt.assert_awaited_once()
    said = ctx.prompt.await_args.args[0]
    assert "Sam could not pick up" in said and "take a message" in said
    assert _posted(api, RESULT_PATH)[0]["outcome"] == "no_answer"


async def test_cancelled_dial_is_recorded_without_a_prompt() -> None:
    """cancel_transfer already gave the agent its turn."""
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("cancelled", None, 4000))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert _posted(api, RESULT_PATH)[0]["outcome"] == "cancelled"
    ctx.prompt.assert_not_awaited()


async def test_second_transfer_while_ringing_is_refused() -> None:
    spec = transfer_call.bind(META)
    ctx, _, _ = _ctx(ROUTE)
    ring = asyncio.Event()

    async def bridge(_route):
        await ring.wait()
        return BridgeOutcome("no_answer", 480, 1)

    ctx.bridge = bridge
    await spec.execute(ctx, {"contact": "Sam"})
    again = await spec.execute(ctx, {"contact": "Sam"})
    assert again == (
        "Sam is still ringing. To try someone else, call cancel_transfer first."
    )
    ring.set()
    await _settled()
    # Once the first dial ended, a new one may start.
    ctx.bridge = AsyncMock(return_value=BridgeOutcome("no_answer", 480, 1))
    assert await spec.execute(ctx, {"contact": "Sam"}) is None
    await _settled()


async def test_denied_by_api() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(
        {"ok": False, "spoken": "I can't connect you to that person right now."}
    )
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said == "I can't connect you to that person right now."
    bridge.assert_not_awaited()
    assert api.post.await_count == 1


async def test_unknown_name() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    said = await spec.execute(ctx, {"contact": "Bob", "reason": "x"})
    assert "Sam" in said
    api.post.assert_not_awaited()


async def test_no_bridge_handle() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    ctx.bridge = None
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said
    api.post.assert_not_awaited()


async def test_result_post_failure_still_connected() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("answered", None, 1))
    api.post.side_effect = _post_by_path(ROUTE, [RuntimeError("boom")])
    assert await spec.execute(ctx, {"contact": "Sam", "reason": "x"}) is None
    await _settled()


async def test_bridge_raises_posts_failed() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE)
    bridge.side_effect = RuntimeError("boom")
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    result = _posted(api, RESULT_PATH)[0]
    assert result["outcome"] == "failed"
    assert result["sip_status"] is None and result["ring_ms"] == 0
    ctx.prompt.assert_awaited_once()


async def test_prompt_failure_never_raises() -> None:
    spec = transfer_call.bind(META)
    ctx, _, _ = _ctx(ROUTE, BridgeOutcome("busy", 486, 1))
    ctx.prompt.side_effect = RuntimeError("session closed")
    await spec.execute(ctx, {"contact": "Sam"})
    await _settled()


def test_bind_ignores_malformed_targets() -> None:
    for bad in ([{}], [{"label": "x"}], ["not-a-dict"]):
        assert transfer_call.bind({"handover_targets": bad}) is None
    mixed = [{}, {"contact_id": CID, "label": "Sam"}, "x"]
    spec = transfer_call.bind({"handover_targets": mixed})
    assert spec.parameters["properties"]["contact"]["enum"] == ["Sam"]


async def test_reason_whitespace_collapsed() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("answered", None, 1))
    await spec.execute(ctx, {"contact": "Sam", "reason": " an \n  invoice "})
    await _settled()
    assert _posted(api, SMS_PATH)[0]["reason"] == "an invoice"


def test_bind_wiring() -> None:
    assert transfer_call.SPEC.bind is transfer_call.bind
    assert transfer_call.bind(META).bind is None


def _answering_bridge(order: list[str], ring_ms: int = 9000):
    """A bridge that reports the answer through ``on_answered`` and then
    mutes the agent, like the voicebot's."""

    async def bridge(route):
        post = asyncio.ensure_future(route.on_answered(ring_ms))
        order.append("muted")
        await post
        return BridgeOutcome("answered", None, ring_ms)

    return bridge


async def test_answered_posted_once() -> None:
    spec = transfer_call.bind(META)
    order: list[str] = []
    ctx, api, _ = _ctx(ROUTE)

    async def post(path, body):
        if path in (SMS_PATH, EMAIL_PATH):
            return {"sms": "sent", "email": "sent"}
        order.append(body.get("outcome") or "route")
        return ROUTE if path.endswith("/handover") else {"ok": True}

    api.post.side_effect = post
    ctx.bridge = _answering_bridge(order)
    assert await spec.execute(ctx, {"contact": "Sam", "reason": "x"}) is None
    await _settled()
    assert order == ["route", "muted", "answered"]
    [result] = _posted(api, RESULT_PATH)
    assert result["ring_ms"] == 9000 and result["contact_id"] == CID
    assert result["sms"] == "sent" and result["email"] == "sent"
    assert "picked_up_by" not in result


async def test_answered_is_posted_at_once_then_the_notices_follow() -> None:
    """The answer lifts the time limit and job shutdown waits only briefly,
    so it never waits on a slow text or email. A second post records them."""
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    base = _post_by_path(ROUTE)

    async def post(path, body):
        if path in (SMS_PATH, EMAIL_PATH):
            await asyncio.sleep(0.02)
        return await base(path, body)

    api.post.side_effect = post
    ctx.bridge = _answering_bridge([])
    await spec.execute(ctx, {"contact": "Sam"})
    await _settled()
    first, update = _posted(api, RESULT_PATH)
    assert first["outcome"] == update["outcome"] == "answered"
    assert first["sms"] == "pending" and first["email"] == "pending"
    assert update["sms"] == "sent" and update["email"] == "sent"
    assert update["tool_invocation_id"] == first["tool_invocation_id"]


async def test_caller_hears_the_no_answer_before_the_bookkeeping() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("busy", 486, 1000))
    order: list[str] = []
    base = _post_by_path(ROUTE)

    async def post(path, body):
        if path == RESULT_PATH:
            order.append("result")
        return await base(path, body)

    async def prompt(_text):
        order.append("prompt")

    api.post.side_effect = post
    ctx.prompt = prompt
    await spec.execute(ctx, {"contact": "Sam"})
    await _settled()
    assert order[0] == "prompt"


async def test_two_transfers_in_one_turn_dial_once() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 1))
    first, second = await asyncio.gather(
        spec.execute(ctx, {"contact": "Sam"}), spec.execute(ctx, {"contact": "Sam"})
    )
    await _settled()
    assert first is None
    assert second == (
        "Sam is still ringing. To try someone else, call cancel_transfer first."
    )
    assert bridge.await_count == 1
    assert (
        len([c for c in api.post.await_args_list if c.args[0].endswith("/handover")])
        == 1
    )


async def test_ring_over_frees_the_tool_before_the_bookkeeping() -> None:
    """Once the dial returned, a new transfer may start even while the
    result post is still going."""
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("busy", 486, 1))
    hold = asyncio.Event()
    base = _post_by_path(ROUTE)
    asked: list = []

    async def post(path, body):
        if path == RESULT_PATH:
            await hold.wait()
        return await base(path, body)

    async def prompt(_text):
        if not asked:  # once: the second dial prompts too
            asked.append(await spec.execute(ctx, {"contact": "Sam"}))

    api.post.side_effect = post
    ctx.prompt = prompt
    await spec.execute(ctx, {"contact": "Sam"})
    while not asked:
        await asyncio.sleep(0)
    assert asked == [None]
    hold.set()
    await _settled()


def test_repeated_names_are_spoken_plainly() -> None:
    meta = {
        "handover_targets": [
            {"contact_id": CID, "label": "Sam", "note": "Billing"},
            {"contact_id": str(uuid.uuid4()), "label": "Sam (2)", "note": "Sales"},
        ]
    }
    assert transfer_call.bind(meta) is not None
    assert transfer_call._spoken("Sam (2)") == "Sam"


async def test_no_answer_names_a_repeated_person_plainly() -> None:
    meta = {"handover_targets": [{"contact_id": CID, "label": "Sam (2)", "note": "x"}]}
    spec = transfer_call.bind(meta)
    ctx, _, bridge = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 1))
    await spec.execute(ctx, {"contact": "Sam (2)"})
    await _settled()
    assert ctx.prompt.await_args.args[0].startswith("Sam could not pick up")
    assert bridge.await_args.args[0].name == "Sam"


async def test_answered_post_retries(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_ANSWERED_BACKOFF", (0, 0))
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(
        ROUTE, [RuntimeError("a"), RuntimeError("b"), {"ok": True}]
    )
    ctx.bridge = _answering_bridge([])
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert len(_posted(api, RESULT_PATH)) == 3


async def test_answered_post_gives_up_after_the_last_attempt(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_ANSWERED_BACKOFF", (0, 0))
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(ROUTE, [RuntimeError("x")] * 5)
    ctx.bridge = _answering_bridge([])
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert len(_posted(api, RESULT_PATH)) == 3


def _slow_bridge(outcome):
    """A dial that takes a moment, like a real one: the text goes out first."""

    async def bridge(_route):
        await asyncio.sleep(0.01)
        return outcome

    return bridge


async def test_texts_and_emails_the_contact_while_dialing() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": " a  leak "})
    await _settled()
    [text] = _posted(api, SMS_PATH)
    [mail] = _posted(api, EMAIL_PATH)
    [result] = _posted(api, RESULT_PATH)
    assert (
        text
        == mail
        == {
            "tool_invocation_id": result["tool_invocation_id"],
            "call_id": str(ctx.call_id),
            "contact_id": CID,
            "reason": "a leak",
        }
    )
    assert result["sms"] == "sent" and result["email"] == "sent"


async def test_email_failure_never_stops_the_dial() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(ROUTE, email_reply=RuntimeError("api down"))
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    result = _posted(api, RESULT_PATH)[0]
    assert result["email"] == "failed" and result["sms"] == "sent"


async def test_blocked_text_is_recorded_and_the_dial_goes_on() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(
        ROUTE, sms_reply={"sms": "blocked:us_registration"}
    )
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert _posted(api, RESULT_PATH)[0]["sms"] == "blocked:us_registration"


async def test_text_failure_never_stops_the_dial() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(ROUTE, sms_reply=RuntimeError("api down"))
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert _posted(api, RESULT_PATH)[0]["sms"] == "failed"
    ctx.prompt.assert_awaited_once()


async def test_slow_notice_is_pending_after_the_wait(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_NOTICE_WAIT_SECONDS", 0.01)
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 1))
    base = _post_by_path(ROUTE)

    async def post(path, body):
        if path == SMS_PATH:
            await asyncio.sleep(0.1)
        return await base(path, body)

    api.post.side_effect = post
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert _posted(api, RESULT_PATH)[0]["sms"] == "pending"
    assert len(_posted(api, SMS_PATH)) == 1 and not transfer_call._pending


async def test_no_notice_when_the_api_refuses_the_handover() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx({"ok": False, "spoken": "no"})
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _settled()
    assert _posted(api, SMS_PATH) == [] and _posted(api, EMAIL_PATH) == []


async def test_cancelled_notice_is_reported_failed() -> None:
    task = asyncio.ensure_future(asyncio.sleep(10))
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert transfer_call._notice_state(task) == "failed"


# --- cancel_transfer ----------------------------------------------------------


async def test_cancel_stops_a_ringing_dial() -> None:
    ctx, _, _ = _ctx(ROUTE)
    ctx.cancel_bridge = AsyncMock(return_value=True)
    said = await transfer_call.CANCEL_SPEC.execute(ctx, {})
    assert said.startswith("The ring is stopped.")
    ctx.cancel_bridge.assert_awaited_once()


async def test_cancel_with_nothing_ringing() -> None:
    ctx, _, _ = _ctx(ROUTE)
    ctx.cancel_bridge = AsyncMock(return_value=False)
    said = await transfer_call.CANCEL_SPEC.execute(ctx, {})
    assert said == "Nothing is ringing, so there is nothing to stop."


async def test_cancel_without_a_handle() -> None:
    ctx, _, _ = _ctx(ROUTE)
    assert await transfer_call.CANCEL_SPEC.execute(ctx, {}) == (
        "I can't connect you to anyone right now."
    )


async def test_finish_pending_waits_for_the_background_posts() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 1))
    await spec.execute(ctx, {"contact": "Sam"})
    await transfer_call.finish_pending(1.0)
    assert _posted(api, RESULT_PATH)
    await _settled()


async def test_finish_pending_gives_up_after_the_timeout() -> None:
    blocked = asyncio.Event()
    task = transfer_call._hold(asyncio.create_task(blocked.wait()))
    await transfer_call.finish_pending(0.01)
    assert not task.done()
    blocked.set()
    await _settled()
