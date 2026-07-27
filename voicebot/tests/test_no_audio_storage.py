"""Regression test for the "no call audio is ever stored" guarantee.

This is a Privacy Policy claim, not just an implementation detail — see
``hailhq/voicebot/recording.py``. It must stay standalone and impossible to
miss (as opposed to being buried inside an unrelated behavioral assertion)
because the moment someone wires up real LiveKit Egress recording, this test
starts failing loudly and points them at the policy language that needs to
change alongside the code.

Every assertion here goes through :func:`on_call_end` — the real
call-completion path (shutdown callback -> upload_recording -> Call row
update) — not by calling ``upload_recording`` directly.
"""

from __future__ import annotations

from uuid import UUID

import pytest
from hailhq.core.models import Call
from hailhq.voicebot.agent import on_call_end
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .test_agent import _make_call_row

# (status_override, end_reason_override) permutations already exercised by
# tests/test_agent.py's on_call_end coverage — every terminal status/end_reason
# combination the voicebot actually produces today.
_CALL_END_PERMUTATIONS: list[tuple[str | None, str | None]] = [
    (None, None),  # happy path -> completed / normal_hangup
    ("no_answer", "user_unavailable"),
    ("busy", "user_rejected"),
    ("failed", "sip_trunk_failure"),
    ("failed", "connection_timeout"),
    ("failed", "media_failure"),
    ("completed", "soft_cap_reached"),
]


@pytest.mark.parametrize("status_override,end_reason_override", _CALL_END_PERMUTATIONS)
async def test_no_audio_ever_stored(
    async_session: AsyncSession,
    status_override: str | None,
    end_reason_override: str | None,
) -> None:
    """No matter how a call ends, no recording ever lands on the Call row.

    v1 has no Egress wiring (hailhq.voicebot.recording.upload_recording is a
    stub that always returns None), so recording_s3_key and
    recording_duration_ms must stay None through the real on_call_end path
    for every status/end_reason permutation the voicebot can produce.
    """
    call_id: UUID = await _make_call_row(async_session)

    await on_call_end(
        call_id,
        room_name=f"hail-{call_id}",
        status_override=status_override,
        end_reason_override=end_reason_override,
    )

    refreshed = (
        await async_session.execute(select(Call).where(Call.id == call_id))
    ).scalar_one()
    await async_session.refresh(refreshed)

    assert refreshed.recording_s3_key is None
    assert refreshed.recording_duration_ms is None
