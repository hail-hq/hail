import asyncio
import uuid
from unittest.mock import AsyncMock

from hailhq.core.agent_tools import transfer_call
from hailhq.core.agent_tools.spec import BridgeOutcome, ToolContext

CID = str(uuid.uuid4())
META = {"handover_targets": [{"contact_id": CID, "label": "Sam", "note": "Billing"}]}


RESULT_PATH = "/internal/agent/handover-result"
SMS_PATH = "/internal/agent/handover-sms"


def _post_by_path(route_reply, result_replies=None, sms_reply=None):
    """``api.post`` by path: the route reply, the text reply, then
    ``result_replies`` in order for the result posts (then ``{"ok": True}``)."""
    results = list(result_replies or [])

    async def post(path, body):
        if path == "/internal/agent/handover":
            return route_reply
        reply = sms_reply if path == SMS_PATH else None
        if path == RESULT_PATH:
            reply = results.pop(0) if results else None
        if isinstance(reply, Exception):
            raise reply
        return reply if reply is not None else {"ok": True, "sms": "sent"}

    return post


def _posted(api, path):
    return [c.args[1] for c in api.post.await_args_list if c.args[0] == path]


async def _texts_done() -> None:
    await asyncio.gather(*transfer_call._texting)


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


async def test_answered() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE, BridgeOutcome("answered", None, 9000))
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "an invoice"})
    assert said is None  # no reply turn: the two people are talking
    route = bridge.await_args.args[0]
    assert route.to_e164 == "+14155550120" and route.reason == "an invoice"
    assert api.post.await_args_list[0].args == (
        "/internal/agent/handover",
        {"call_id": str(ctx.call_id), "contact_id": CID},
    )
    result = _posted(api, RESULT_PATH)[0]
    assert result["outcome"] == "answered" and result["ring_ms"] == 9000


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


async def test_no_answer_posts_the_pickup_verdict() -> None:
    """A voicemail the check caught: the verdict reaches the result post."""
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("no_answer", None, 24000, "machine-vm"))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    result = _posted(api, RESULT_PATH)[0]
    assert result["outcome"] == "no_answer"
    assert result["picked_up_by"] == "machine-vm"


async def test_no_answer_comes_back() -> None:
    spec = transfer_call.bind(META)
    ctx, _, _ = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 30000))
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert "could not pick up" in said


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


async def test_bridge_raises_posts_failed() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE)
    bridge.side_effect = RuntimeError("boom")
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert "could not pick up" in said
    result = _posted(api, RESULT_PATH)[0]
    assert result["outcome"] == "failed"
    assert result["sip_status"] is None and result["ring_ms"] == 0


def test_bind_ignores_malformed_targets() -> None:
    for bad in ([{}], [{"label": "x"}], ["not-a-dict"]):
        assert transfer_call.bind({"handover_targets": bad}) is None
    mixed = [{}, {"contact_id": CID, "label": "Sam"}, "x"]
    spec = transfer_call.bind({"handover_targets": mixed})
    assert spec.parameters["properties"]["contact"]["enum"] == ["Sam"]


async def test_reason_whitespace_collapsed() -> None:
    spec = transfer_call.bind(META)
    ctx, _, bridge = _ctx(ROUTE, BridgeOutcome("answered", None, 1))
    await spec.execute(ctx, {"contact": "Sam", "reason": " an \n  invoice "})
    assert bridge.await_args.args[0].reason == "an invoice"


def test_bind_wiring() -> None:
    assert transfer_call.SPEC.bind is transfer_call.bind
    assert transfer_call.bind(META).bind is None


def _answering_bridge(order: list[str], ring_ms: int = 9000):
    """A bridge that reports the answer through ``on_answered`` and then
    plays the intro, like the voicebot's."""

    async def bridge(route):
        await route.on_answered(ring_ms, "human")
        order.append("intro")
        return BridgeOutcome("answered", None, ring_ms, "human")

    return bridge


async def test_answered_posted_before_intro_and_once() -> None:
    spec = transfer_call.bind(META)
    order: list[str] = []
    ctx, api, _ = _ctx(ROUTE)

    async def post(path, body):
        if path == SMS_PATH:
            return {"sms": "sent"}
        order.append(body.get("outcome") or "route")
        return ROUTE if path.endswith("/handover") else {"ok": True}

    api.post.side_effect = post
    ctx.bridge = _answering_bridge(order)
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said is None
    assert order == ["route", "answered", "intro"]
    result = _posted(api, RESULT_PATH)[0]
    assert result["ring_ms"] == 9000 and result["contact_id"] == CID
    assert result["picked_up_by"] == "human"


async def test_answered_post_retries(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_ANSWERED_BACKOFF", (0, 0))
    spec = transfer_call.bind(META)
    order: list[str] = []
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(
        ROUTE, [RuntimeError("a"), RuntimeError("b"), {"ok": True}]
    )
    ctx.bridge = _answering_bridge(order)
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said is None
    assert len(_posted(api, RESULT_PATH)) == 3


async def test_answered_post_gives_up_after_the_last_attempt(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_ANSWERED_BACKOFF", (0, 0))
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(ROUTE, [RuntimeError("x")] * 5)
    ctx.bridge = _answering_bridge([])
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said is None
    assert len(_posted(api, RESULT_PATH)) == 3


def _slow_bridge(outcome):
    """A dial that takes a moment, like a real one: the text goes out first."""

    async def bridge(_route):
        await asyncio.sleep(0.01)
        return outcome

    return bridge


async def test_texts_the_contact_while_dialing() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": " a  leak "})
    [text] = _posted(api, SMS_PATH)
    [result] = _posted(api, RESULT_PATH)
    assert text == {
        "tool_invocation_id": result["tool_invocation_id"],
        "call_id": str(ctx.call_id),
        "contact_id": CID,
        "reason": "a leak",
    }
    assert result["sms"] == "sent"


async def test_blocked_text_is_recorded_and_the_dial_goes_on() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(
        ROUTE, sms_reply={"sms": "blocked:us_registration"}
    )
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert _posted(api, RESULT_PATH)[0]["sms"] == "blocked:us_registration"


async def test_text_failure_never_stops_the_dial() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = _post_by_path(ROUTE, sms_reply=RuntimeError("api down"))
    ctx.bridge = _slow_bridge(BridgeOutcome("no_answer", 480, 30000))
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert "could not pick up" in said
    assert _posted(api, RESULT_PATH)[0]["sms"] == "failed"


async def test_text_still_running_is_pending_and_finishes() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE, BridgeOutcome("no_answer", 480, 1))
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert _posted(api, RESULT_PATH)[0]["sms"] == "pending"
    await _texts_done()
    assert len(_posted(api, SMS_PATH)) == 1 and not transfer_call._texting


async def test_no_text_when_the_api_refuses_the_handover() -> None:
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx({"ok": False, "spoken": "no"})
    await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    await _texts_done()
    assert _posted(api, SMS_PATH) == []
