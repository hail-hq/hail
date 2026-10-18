"""Workspaces that have not opted in never retain call audio.

Optional recording has its own lifecycle and tests in test_recording.py.
These checks cover the unchanged, disabled-by-default call finalizer.
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
async def test_audio_off_without_workspace_opt_in(
    async_session: AsyncSession,
    status_override: str | None,
    end_reason_override: str | None,
) -> None:
    """Default calls retain no audio through every call-completion path."""
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
