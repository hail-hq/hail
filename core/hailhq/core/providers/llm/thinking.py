"""How each Anthropic model turns thinking off. Used by the voicebot's
house LLM and by the document reader, so both send each model the value it
takes."""

from __future__ import annotations

import logging
import re

__all__ = ["THINKING_OFF", "base_model_id", "thinking_off"]

logger = logging.getLogger(__name__)

# Sonnet 5.5 rejects ``disabled`` and takes ``between_tools``; Opus 5.5
# cannot turn it off (None: send no thinking value). Keys are first-party
# ids; ``base_model_id`` maps a Bedrock id onto them.
THINKING_OFF: dict[str, dict[str, str] | None] = {
    "claude-sonnet-5-5": {"type": "between_tools"},
    "claude-haiku-5-5": {"type": "disabled"},
    "claude-opus-5-5": None,
}

_warned_models: set[str] = set()


def base_model_id(model: str) -> str:
    """``eu.anthropic.claude-sonnet-5-5-v1:0`` -> ``claude-sonnet-5-5``: drop
    the region prefix, ``anthropic.``, a date (``-20260101``) and the
    version suffix."""
    base = model.rsplit("anthropic.", 1)[-1]
    return re.sub(r"(-\d{8})?(-v\d+)?(:\d+)?$", "", base)


def thinking_off(model: str) -> dict[str, str] | None:
    """The model's thinking-off value, or None when the model cannot turn
    thinking off (send no thinking value then). An id missing from the
    table gets ``disabled`` and one warning: a lookup must never stop a
    call."""
    base = base_model_id(model)
    if base in THINKING_OFF:
        return THINKING_OFF[base]
    if base not in _warned_models:
        _warned_models.add(base)
        logger.warning(
            "model %r has no entry in THINKING_OFF "
            "(core/hailhq/core/providers/llm/thinking.py); using thinking disabled",
            model,
        )
    return {"type": "disabled"}
