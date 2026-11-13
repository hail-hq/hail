"""Agent-input filtering, separate from room recording and published tracks.

Uses the supported LiveKit input-filter selector with Agents 1.8.8:
https://docs.livekit.io/transport/media/noise-cancellation/
"""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.util import find_spec

from hailhq.core.config import settings
from livekit import rtc
from livekit.agents import room_io
from livekit.agents.utils.misc import is_cloud

logger = logging.getLogger("hailhq.voicebot")
FILTERS = ("bvc", "krisp_viva", "voice_focus_s", "voice_focus_l")


_MODULES = {
    "bvc": "livekit.plugins.noise_cancellation",
    "krisp_viva": "livekit.plugins.krisp",
    "voice_focus_s": "livekit.plugins.ai_coustics",
    "voice_focus_l": "livekit.plugins.ai_coustics",
}


def filter_choice() -> str:
    """The configured filter; bvc when it is unknown or its plugin is missing."""
    choice = settings.hail_audio_filter.strip().lower()
    if choice not in FILTERS:
        logger.warning("unknown HAIL_AUDIO_FILTER %r; using bvc", choice)
        return "bvc"
    if choice != "bvc" and find_spec(_MODULES[choice]) is None:
        logger.warning(
            "HAIL_AUDIO_FILTER=%s needs the voicebot 'filters' extra; using bvc",
            choice,
        )
        return "bvc"
    return choice


def _cloud_filter():
    """(choice, plugin module) for LiveKit Cloud, or None off Cloud.

    A non-default filter that cannot load never stops calls: bvc is used.
    """
    if not is_cloud(settings.livekit_url):
        return None
    choice = filter_choice()
    if choice != "bvc":
        try:
            return choice, import_module(_MODULES[choice])
        except Exception:
            logger.exception("HAIL_AUDIO_FILTER=%s failed to load; using bvc", choice)
    return "bvc", import_module(_MODULES["bvc"])


def prewarm_audio() -> None:
    """Load the native Cloud filter before the job's event loop serves a call."""
    _cloud_filter()


def _build(choice: str, plugin, is_sip: bool):
    if choice == "krisp_viva":
        if is_sip:
            return plugin.voice_isolation_telephony()
        return plugin.voice_isolation()
    if choice in {"voice_focus_s", "voice_focus_l"}:
        model = (
            plugin.EnhancerModel.QUAIL_VF_S
            if choice == "voice_focus_s"
            else plugin.EnhancerModel.QUAIL_VF_L
        )
        return plugin.audio_enhancement(model=model)
    return plugin.BVCTelephony() if is_sip else plugin.BVC()


def room_options() -> room_io.RoomOptions:
    selector = None
    loaded = _cloud_filter()
    if loaded is not None:
        choice, plugin = loaded
        # It processes subscribed input frames without republishing cleaned audio;
        # composite Egress continues to capture the original room tracks.

        def selector(params):
            is_sip = params.participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_SIP
            if choice == "bvc":
                return _build(choice, plugin, is_sip)
            try:
                return _build(choice, plugin, is_sip)
            except Exception:
                logger.exception(
                    "HAIL_AUDIO_FILTER=%s failed for this call; using bvc", choice
                )
                return _build("bvc", import_module(_MODULES["bvc"]), is_sip)

    return room_io.RoomOptions(
        audio_input=room_io.AudioInputOptions(noise_cancellation=selector)
    )
