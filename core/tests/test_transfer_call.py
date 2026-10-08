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
    ctx, api, bridge = _ctx({"ok": False, "spoken": "I can't connect you to that person right now."})
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
