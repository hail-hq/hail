"""Agent-input filtering, separate from room recording and published tracks.

Uses the supported LiveKit input-filter selector with Agents 1.8.8:
https://docs.livekit.io/transport/media/noise-cancellation/
"""

from __future__ import annotations

import logging
from importlib import import_module

from hailhq.core.config import settings
from livekit import rtc
from livekit.agents import room_io
from livekit.agents.utils.misc import is_cloud

logger = logging.getLogger("hailhq.voicebot")
FILTERS = ("bvc", "krisp_viva", "voice_focus_s", "voice_focus_l")


def filter_choice() -> str:
    """The configured filter; an unknown value falls back to bvc with a warning."""
    choice = settings.hail_audio_filter.strip().lower()
    if choice in FILTERS:
        return choice
    logger.warning("unknown HAIL_AUDIO_FILTER %r; using bvc", choice)
    return "bvc"


def _cloud_filter():
    """The proprietary Cloud filter module, or None off LiveKit Cloud."""
    if not is_cloud(settings.livekit_url):
        return None
    choice = filter_choice()
    if choice == "krisp_viva":
        return import_module("livekit.plugins.krisp")
    if choice in {"voice_focus_s", "voice_focus_l"}:
        return import_module("livekit.plugins.ai_coustics")
    return import_module("livekit.plugins.noise_cancellation")


def prewarm_audio() -> None:
    """Load the native Cloud filter before the job's event loop serves a call."""
    _cloud_filter()


def room_options() -> room_io.RoomOptions:
    selector = None
    noise_cancellation = _cloud_filter()
    if noise_cancellation is not None:
        choice = filter_choice()
        # It processes subscribed input frames without republishing cleaned audio;
        # composite Egress continues to capture the original room tracks.

        def selector(params):
            is_sip = params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            if choice == "krisp_viva":
                if is_sip:
                    return noise_cancellation.voice_isolation_telephony()
                return noise_cancellation.voice_isolation()
            if choice in {"voice_focus_s", "voice_focus_l"}:
                model = (
                    noise_cancellation.EnhancerModel.QUAIL_VF_S
                    if choice == "voice_focus_s"
                    else noise_cancellation.EnhancerModel.QUAIL_VF_L
                )
                return noise_cancellation.audio_enhancement(model=model)
            if is_sip:
                return noise_cancellation.BVCTelephony()
            return noise_cancellation.BVC()

    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(noise_cancellation=selector)
    )
