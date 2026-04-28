"""Call-recording upload helpers.

v1 ships a stub: real LiveKit Egress wiring (track-composite egress to S3,
completion webhooks, deferred metadata reconciliation) is non-trivial and
deferred. The voicebot still calls :func:`upload_recording` on hangup so the
agent shape stays correct; the result (``None`` in v1) is stored in
``Call.recording_s3_key`` unchanged.
"""

from __future__ import annotations

import logging
from uuid import UUID

logger = logging.getLogger(__name__)


async def upload_recording(call_id: UUID, room_name: str) -> str | None:
    """Upload (or, in v1, no-op) a call recording for ``call_id``.

    Returns the S3 object key on success, or ``None`` if recording is not
    enabled for this deploy. v1 always returns ``None``.
    """
    logger.info(
        "recording.upload_recording: stub — no Egress wired (call_id=%s, room=%s)",
        call_id,
        room_name,
    )
    return None
