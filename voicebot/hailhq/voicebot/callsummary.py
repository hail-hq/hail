"""Call analysis worker: sends ``call.analyzed`` with a short AI summary.

Runs as one more poller in the text worker thread (``textbot.run_forever``),
so the summary uses the same LLM precedence and keys as texts:
``build_llm(None, org_llm)`` (the org's own LLM, with Hail's chain as
fallback when the org turned it on, else Hail's chain). A per-call ``llm``
from ``POST /v1/calls`` is not stored, so it is not used here.

The DB side lives in ``hailhq.core.call_analysis``. A call is claimed before
the model runs, so two voicebot replicas never summarize the same call. No DB
session is open while the model runs. A failed summary never blocks the event: it is sent
with ``summary`` null and a warning is logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any

import logfire
from hailhq.core.call_analysis import (
    ANALYSIS_WINDOW,
    call_transcript,
    claim_call_to_analyze,
    send_call_analyzed,
)
from hailhq.core.db import session_scope
from hailhq.core.prompts import CALL_SUMMARY_INSTRUCTIONS
from hailhq.voicebot.chat_loop import LLM_TIMEOUT_SECONDS, run_chat
from hailhq.voicebot.pipeline import build_llm, resolve_org_configs
from livekit.agents.llm import FunctionToolCall

logger = logging.getLogger("hailhq.voicebot.callsummary")

__all__ = ["MAX_SEND_ATTEMPTS", "MAX_SUMMARY_CHARS", "analyze_once", "summarize"]

MAX_SUMMARY_CHARS = 1000
# The model reads at most this much of a long call: its start and its end.
MAX_PROMPT_CHARS = 60_000
# Failed tries (a DB error) per call before this process stops trying it.
MAX_SEND_ATTEMPTS = 3
_ROLE_LABELS = {"assistant": "Agent", "user": "Person"}


@dataclass
class _Failure:
    count: int
    first: float  # monotonic time of the first failure
    summary: str | None = None  # kept, so a retry does not run the model again
    summarized: bool = False


# Calls whose last try failed. In memory only: a restart tries such a call
# again, while it is inside the window. A failed call keeps its claim, so the
# next try comes after ``CLAIM_LEASE``.
_failures: dict[uuid.UUID, _Failure] = {}


def _transcript_text(turns: list[dict[str, Any]]) -> str:
    text = "\n".join(f"{_ROLE_LABELS[t['role']]}: {t['text']}" for t in turns)
    if len(text) <= MAX_PROMPT_CHARS:
        return text
    half = MAX_PROMPT_CHARS // 2
    return f"{text[:half]}\n[...]\n{text[-half:]}"


async def _no_tools(call: FunctionToolCall) -> tuple[str, bool]:
    # run_chat gets no tools, so the model cannot call one.
    return "", True


async def summarize(
    organization_id: uuid.UUID, call_id: uuid.UUID, turns: list[dict[str, Any]]
) -> str | None:
    """A short summary of the call, or None when the model fails for any
    reason (no key, a bad org LLM, a timeout, an empty answer)."""
    try:
        org_cfgs = await resolve_org_configs(organization_id)
        llm = build_llm(None, org_cfgs.get("llm"))
        messages = [
            {"role": "system", "content": CALL_SUMMARY_INSTRUCTIONS},
            {"role": "user", "content": _transcript_text(turns)},
        ]
        text = await asyncio.wait_for(
            run_chat(
                llm,
                messages,
                [],
                _no_tools,
                max_rounds=1,
                log_label=f"call_id={call_id}",
            ),
            timeout=LLM_TIMEOUT_SECONDS,
        )
    except Exception as exc:
        logger.warning(
            "call_id=%s call summary failed, sending without it: %s: %s",
            call_id,
            type(exc).__name__,
            exc,
        )
        return None
    text = text.strip()[:MAX_SUMMARY_CHARS]
    if not text:
        logger.warning("call_id=%s call summary was empty", call_id)
        return None
    return text


def _given_up() -> set[uuid.UUID]:
    """Calls this process stopped trying. Forgets entries older than the
    window (those calls are no longer picked anyway)."""
    cutoff = time.monotonic() - ANALYSIS_WINDOW.total_seconds()
    for call_id, failure in list(_failures.items()):
        if failure.first < cutoff:
            del _failures[call_id]
    return {cid for cid, f in _failures.items() if f.count >= MAX_SEND_ATTEMPTS}


async def analyze_once(_api: Any = None) -> bool:
    """Send ``call.analyzed`` for one call. Returns False when none waited.
    ``_api`` is unused: the poller hands every worker the API client.

    Any error after a call is picked counts against that call, so one bad
    call cannot hold the queue: after ``MAX_SEND_ATTEMPTS`` this process
    skips it."""
    with logfire.suppress_instrumentation():
        async with session_scope() as db:
            call = await claim_call_to_analyze(db, exclude=_given_up())
            if call is None:
                return False
            call_id, org_id = call.id, call.organization_id
    failure = _failures.get(call_id)
    summarized = failure is not None and failure.summarized
    summary = failure.summary if failure is not None else None
    try:
        async with session_scope() as db:
            turns = await call_transcript(db, call_id)
        if not summarized:
            # Empty after filtering: no model call; the marker alone is written.
            summary = await summarize(org_id, call_id, turns) if turns else None
            summarized = True
        async with session_scope() as db:
            await send_call_analyzed(db, call_id, turns, summary)
    except Exception:
        if failure is None:
            failure = _failures[call_id] = _Failure(0, time.monotonic())
        failure.count += 1
        failure.summary, failure.summarized = summary, summarized
        raise
    _failures.pop(call_id, None)
    return True
