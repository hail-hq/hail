"""Answering machine detection for outbound calls.

Classifies who picked up — person, phone tree, voicemail, dead mailbox — so
the agent never talks to a machine. Lives outside :mod:`hailhq.voicebot.agent`
purely to keep that module's size in check; the only caller is ``entrypoint``.

Verified 2026-07-21 against livekit-agents 1.6.6
(``livekit/agents/voice/amd/detector.py``, ``.../classifier.py``): the ``AMD``
constructor kwargs used below, ``AMDCategory`` values, and ``execute()``
returning an :class:`AMDPredictionEvent`.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

from livekit.agents.llm import LLM
from livekit.agents.voice import AgentSession
from livekit.agents.voice.amd import AMD, AMDPredictionEvent

from hailhq.core.call_end_reasons import CallEndReason

logger = logging.getLogger("hailhq.voicebot")

# Categories we hang up on rather than speak to. `machine-ivr` is deliberately
# absent — AMD starts LiveKit's IVR navigation for it and the tree may still
# route to a person. `uncertain` is absent too: when in doubt, talk.
MACHINE_HANGUP_CATEGORIES: frozenset[str] = frozenset(
    {"machine-vm", "machine-unavailable"}
)

_END_REASON_BY_CATEGORY: dict[str, CallEndReason] = {
    "machine-vm": CallEndReason.VOICEMAIL_REACHED,
    "machine-unavailable": CallEndReason.MACHINE_UNAVAILABLE,
}

# Ceiling on how long a live-but-silent callee waits before hearing the
# disclosure. LiveKit's default is 10.0s; ten seconds of dead air is a worse
# regression than a slightly less certain verdict.
NO_SPEECH_THRESHOLD_SECONDS = 6.0

# Backstop on the whole detection. AMD's own 20s budget is armed only once
# the SIP audio track publishes (``AMD._setup`` awaits
# ``wait_for_track_publication``, which takes no timeout), so a leg that
# never publishes — trunk failure, no early media — would otherwise leave
# ``execute()`` waiting on its verdict forever, with entrypoint blocked
# behind it. Comfortably above AMD's internal budget so this fires only when
# that budget never started.
DETECTION_TIMEOUT_SECONDS = 30.0


def amd_end_reason(category: str) -> str:
    """The ``call_end_reason`` for a machine category we hang up on."""
    return _END_REASON_BY_CATEGORY[category].value


async def run_amd(session: AgentSession, call_id: UUID) -> AMDPredictionEvent | None:
    """Classify the greeting on ``session``'s SIP leg. ``None`` if AMD failed.

    ``llm`` and ``stt`` are passed explicitly on purpose. Left unset on 1.6.6,
    AMD auto-selects LiveKit Inference (``google/gemini-3.1-flash-lite`` +
    ``cartesia/ink-whisper``) whenever ``LIVEKIT_URL`` is Cloud and the API key
    and secret are set — which is Hail's deployed config. That would send the
    greeting transcript to a vendor we do not otherwise use, bill it outside
    the ``usage_events`` model, and override the BYO provider precedence that
    :mod:`hailhq.voicebot.pipeline` exists to enforce. Passing the session's
    own layers keeps classification on the brain the org chose — and since that
    LLM is not on LiveKit's evaluated list, the compatibility warning would
    otherwise fire on every call.

    ``participant_identity`` matches the identity the API service gives the SIP
    participant it creates (``caller-{call_id}``). Without it AMD attaches to
    the first remote audio track, which is non-deterministic if the room ever
    holds another participant.

    A detection failure must never kill a call — same posture as
    ``build_tools_safely``. The caller treats ``None`` as "proceed normally".
    """
    try:
        # session.llm is typed LLM | RealtimeModel | None; Hail never uses a
        # RealtimeModel, so narrow rather than widen AMD's parameter type.
        session_llm = session.llm
        if not isinstance(session_llm, LLM):
            logger.warning("call_id=%s session has no plain LLM; skipping AMD", call_id)
            return None
        session_stt = session.stt
        if session_stt is None:
            logger.warning("call_id=%s session has no STT; skipping AMD", call_id)
            return None
        async with AMD(
            session,
            llm=session_llm,
            stt=session_stt,
            participant_identity=f"caller-{call_id}",
            detection_options={"no_speech_threshold": NO_SPEECH_THRESHOLD_SECONDS},
            suppress_compatibility_warning=True,
        ) as detector:
            return await asyncio.wait_for(
                detector.execute(), timeout=DETECTION_TIMEOUT_SECONDS
            )
    except asyncio.TimeoutError:
        logger.warning(
            "call_id=%s answering machine detection timed out after %ss; "
            "proceeding as if a human answered",
            call_id,
            DETECTION_TIMEOUT_SECONDS,
        )
        return None
    except Exception:
        logger.exception("call_id=%s answering machine detection failed", call_id)
        return None


__all__ = [
    "DETECTION_TIMEOUT_SECONDS",
    "MACHINE_HANGUP_CATEGORIES",
    "NO_SPEECH_THRESHOLD_SECONDS",
    "amd_end_reason",
    "run_amd",
]
