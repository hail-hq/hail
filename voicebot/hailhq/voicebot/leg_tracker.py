"""The SIP participants of one call, kept as carrier legs.

Each SIP participant in the room is one phone line the carrier bills: the
caller (or the person Hail dialed), and each handover dial (``human-*``).
The tracker notes the ids that find the leg in the carrier's records and
when it joined, was answered and left. The call's shutdown hands the legs
to ``hailhq.core.call_legs.record_call_legs``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from hailhq.core.call_legs import LegRecord
from livekit import rtc

SIP_CALL_STATUS_ACTIVE = "active"


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LegTracker:
    def __init__(self, inbound: bool, handover_prefix: str = "human-") -> None:
        self._inbound = inbound
        self._handover_prefix = handover_prefix
        self._legs: dict[str, dict] = {}

    def seen(self, participant: rtc.Participant) -> None:
        """A SIP participant joined or its attributes changed."""
        if participant.kind != rtc.ParticipantKind.PARTICIPANT_KIND_SIP:
            return
        attrs = participant.attributes or {}
        leg = self._legs.get(participant.identity)
        if leg is None:
            handover = participant.identity.startswith(self._handover_prefix)
            leg = self._legs[participant.identity] = {
                "role": "handover" if handover else "call",
                "direction": (
                    "inbound" if self._inbound and not handover else "outbound"
                ),
                "joined_at": _now(),
                "answered_at": None,
                "left_at": None,
            }
        # Attributes can arrive after the join; keep the latest non-empty.
        for key, attr in (
            ("sip_call_id", "sip.callIDFull"),
            ("carrier_call_id", "sip.twilio.callSid"),
        ):
            if attrs.get(attr):
                leg[key] = attrs[attr]
        # Inbound: the number the caller dialed. Outbound: the number dialed.
        number = attrs.get(
            "sip.trunkPhoneNumber"
            if leg["direction"] == "inbound"
            else "sip.phoneNumber"
        )
        if number:
            leg["to_e164"] = number
        if (
            leg["answered_at"] is None
            and attrs.get("sip.callStatus") == SIP_CALL_STATUS_ACTIVE
        ):
            leg["answered_at"] = _now()

    def left(self, participant: rtc.Participant) -> None:
        leg = self._legs.get(participant.identity)
        if leg is not None and leg["left_at"] is None:
            self.seen(participant)
            leg["left_at"] = _now()

    def records(self) -> list[LegRecord]:
        """Every leg seen so far. A leg still in the room ends now."""
        now = _now()
        return [
            LegRecord(
                role=leg["role"],
                direction=leg["direction"],
                sip_call_id=leg.get("sip_call_id"),
                carrier_call_id=leg.get("carrier_call_id"),
                to_e164=leg.get("to_e164"),
                joined_at=leg["joined_at"],
                answered_at=leg["answered_at"],
                left_at=leg["left_at"] or now,
            )
            for leg in self._legs.values()
        ]
