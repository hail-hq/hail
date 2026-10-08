import uuid
from unittest.mock import AsyncMock

from hailhq.core.agent_tools import transfer_call
from hailhq.core.agent_tools.spec import BridgeOutcome, ToolContext

CID = str(uuid.uuid4())
META = {"handover_targets": [{"contact_id": CID, "label": "Sam", "note": "Billing"}]}


def _ctx(api_reply, outcome=None):
    api = AsyncMock()
    api.post.side_effect = [api_reply, {"ok": True}]
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
    assert "connected" in said.lower()
    route = bridge.await_args.args[0]
    assert route.to_e164 == "+14155550120" and route.reason == "an invoice"
    assert api.post.await_args_list[0].args == (
        "/internal/agent/handover",
        {"call_id": str(ctx.call_id), "contact_id": CID},
    )
    result = api.post.await_args_list[1].args[1]
    assert result["outcome"] == "answered" and result["ring_ms"] == 9000
    assert "+1415" not in said


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
    api.post.side_effect = [ROUTE, RuntimeError("boom")]
    assert await spec.execute(ctx, {"contact": "Sam", "reason": "x"}) == (
        transfer_call._CONNECTED
    )


def test_connected_line_tells_the_model_to_stay_silent() -> None:
    """After the tool returns, the LLM gets a turn with end_call still bound;
    the result must tell it to say nothing and not hang up."""
    assert transfer_call._CONNECTED == (
        "Connected. They are talking now. Say nothing and do not end the call."
    )


async def test_bridge_raises_posts_failed() -> None:
    spec = transfer_call.bind(META)
    ctx, api, bridge = _ctx(ROUTE)
    bridge.side_effect = RuntimeError("boom")
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert "could not pick up" in said
    result = api.post.await_args_list[1].args[1]
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
        await route.on_answered(ring_ms)
        order.append("intro")
        return BridgeOutcome("answered", None, ring_ms)

    return bridge


async def test_answered_posted_before_intro_and_once() -> None:
    spec = transfer_call.bind(META)
    order: list[str] = []
    ctx, api, _ = _ctx(ROUTE)

    async def post(path, body):
        order.append(body.get("outcome") or "route")
        return ROUTE if path.endswith("/handover") else {"ok": True}

    api.post.side_effect = post
    ctx.bridge = _answering_bridge(order)
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said == transfer_call._CONNECTED
    assert order == ["route", "answered", "intro"]
    result = api.post.await_args_list[1].args[1]
    assert result["ring_ms"] == 9000 and result["contact_id"] == CID


async def test_answered_post_retries(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_ANSWERED_BACKOFF", (0, 0))
    spec = transfer_call.bind(META)
    order: list[str] = []
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = [ROUTE, RuntimeError("a"), RuntimeError("b"), {"ok": True}]
    ctx.bridge = _answering_bridge(order)
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said == transfer_call._CONNECTED
    assert api.post.await_count == 4


async def test_answered_post_gives_up_after_the_last_attempt(monkeypatch) -> None:
    monkeypatch.setattr(transfer_call, "_ANSWERED_BACKOFF", (0, 0))
    spec = transfer_call.bind(META)
    ctx, api, _ = _ctx(ROUTE)
    api.post.side_effect = [ROUTE] + [RuntimeError("x")] * 5
    ctx.bridge = _answering_bridge([])
    said = await spec.execute(ctx, {"contact": "Sam", "reason": "x"})
    assert said == transfer_call._CONNECTED
    assert api.post.await_count == 4
