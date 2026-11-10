"""Agent-input filtering, separate from room recording and published tracks.

Uses the supported LiveKit input-filter selector with Agents 1.8.8:
https://docs.livekit.io/transport/media/noise-cancellation/
"""

from __future__ import annotations

from importlib import import_module

from hailhq.core.config import settings
from livekit import rtc
from livekit.agents import room_io
from livekit.agents.utils.misc import is_cloud


def _cloud_filter():
    """The proprietary Cloud filter module, or None off LiveKit Cloud."""
    if not is_cloud(settings.livekit_url):
        return None
    if settings.hail_audio_filter == "krisp_viva":
        return import_module("livekit.plugins.krisp")
    if settings.hail_audio_filter in {"voice_focus_s", "voice_focus_l"}:
        return import_module("livekit.plugins.ai_coustics")
    return import_module("livekit.plugins.noise_cancellation")


def prewarm_audio() -> None:
    """Load the native Cloud filter before the job's event loop serves a call."""
    _cloud_filter()


def room_options() -> room_io.RoomOptions:
    selector = None
    noise_cancellation = _cloud_filter()
    if noise_cancellation is not None:
        # It processes subscribed input frames without republishing cleaned audio;
        # composite Egress continues to capture the original room tracks.

        def selector(params):
            is_sip = params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            if settings.hail_audio_filter == "krisp_viva":
                if is_sip:
                    return noise_cancellation.voice_isolation_telephony()
                return noise_cancellation.voice_isolation()
            if settings.hail_audio_filter in {"voice_focus_s", "voice_focus_l"}:
                model = (
                    noise_cancellation.EnhancerModel.QUAIL_VF_S
                    if settings.hail_audio_filter == "voice_focus_s"
                    else noise_cancellation.EnhancerModel.QUAIL_VF_L
                )
                return noise_cancellation.audio_enhancement(model=model)
            if is_sip:
                return noise_cancellation.BVCTelephony()
            return noise_cancellation.BVC()

    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(noise_cancellation=selector)
    )
