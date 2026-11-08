"""LegTracker: each SIP participant becomes one carrier leg."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from hailhq.voicebot.agent import human_identity, is_handover_leg
from hailhq.voicebot.leg_tracker import LegTracker
from livekit import rtc

SIP = rtc.ParticipantKind.PARTICIPANT_KIND_SIP
CALL_ID = uuid4()


def _tracker(inbound: bool) -> LegTracker:
    return LegTracker(inbound, lambda identity: is_handover_leg(identity, CALL_ID))


def _p(identity: str, kind=SIP, **attrs) -> SimpleNamespace:
    return SimpleNamespace(identity=identity, kind=kind, attributes=dict(attrs))


def test_inbound_caller_and_handover_legs():
    legs = _tracker(True)
    caller = _p(
        "sip_+33612345678",
        **{
            "sip.callIDFull": "c89b2bfa",
            "sip.twilio.callSid": "CA1",
            "sip.trunkPhoneNumber": "+14155550100",
            "sip.callStatus": "active",
        },
    )
    legs.seen(caller)
    ringing = _p(
        human_identity(CALL_ID),
        **{"sip.phoneNumber": "+33698765432", "sip.callStatus": "ringing"},
    )
    legs.seen(ringing)
    # Attributes arrive after the join.
    ringing.attributes["sip.callIDFull"] = "out-1"
    legs.seen(ringing)
    legs.left(ringing)
    legs.left(caller)

    call, handover = legs.records()
    assert (call.role, call.direction, call.sip_call_id, call.carrier_call_id) == (
        "call",
        "inbound",
        "c89b2bfa",
        "CA1",
    )
    assert call.to_e164 == "+14155550100"
    assert call.answered_at is not None and call.left_at is not None
    assert (handover.role, handover.direction, handover.sip_call_id) == (
        "handover",
        "outbound",
        "out-1",
    )
    assert handover.to_e164 == "+33698765432"
    # Nobody picked up: no answer time, so no talk time to bill.
    assert handover.answered_at is None


def test_outbound_call_leg_is_answered_on_active():
    legs = _tracker(False)
    callee = _p("sip_callee", **{"sip.phoneNumber": "+15125550123"})
    legs.seen(callee)
    assert legs.records()[0].answered_at is None
    callee.attributes["sip.callStatus"] = "active"
    legs.seen(callee)
    (leg,) = legs.records()
    assert (leg.direction, leg.to_e164) == ("outbound", "+15125550123")
    assert leg.answered_at is not None


def test_ignores_the_agent_and_unknown_leavers():
    legs = _tracker(True)
    legs.seen(_p("agent", kind=rtc.ParticipantKind.PARTICIPANT_KIND_AGENT))
    legs.left(_p("never-seen"))
    assert legs.records() == []


def test_a_leg_still_in_the_room_ends_at_records():
    legs = _tracker(True)
    legs.seen(_p("sip_caller"))
    (leg,) = legs.records()
    assert leg.left_at is not None and leg.left_at >= leg.joined_at
