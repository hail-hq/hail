"""Text worker: the agent answers inbound SMS.

Runs inside the voicebot process (``main.py`` starts it in a daemon thread
with its own event loop) so text replies use the same LLM precedence and
keys as calls: ``build_llm(None, org_llm)`` (org BYO with optional fallback,
else the house chain). The DB side lives in ``hailhq.core.text_agent``; the
send goes through ``POST /internal/agent/reply-sms`` where the thread cap,
funds, suppression, billing and delivery live.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from hailhq.core.agent_tools.client import AgentApiClient
from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.text_agent import (
    MAX_REPLY_CHARS,
    ClaimedReply,
    build_chat_messages,
    claim_pending_reply,
    finish_reply,
    thread_messages,
)
from hailhq.voicebot.pipeline import ProviderKeyError, build_llm, resolve_org_configs
from livekit.agents.llm import ChatContext

logger = logging.getLogger("hailhq.voicebot.textbot")

POLL_SECONDS = 2.0
LLM_TIMEOUT_SECONDS = 30.0

__all__ = [
    "POLL_SECONDS",
    "generate_reply",
    "reply_once",
    "run_forever",
    "start_thread",
]


def _chat_context(messages: list[dict[str, Any]]) -> ChatContext:
    ctx = ChatContext.empty()
    for m in messages:
        ctx.add_message(role=m["role"], content=m["content"])
    return ctx


async def generate_reply(claimed: ClaimedReply, messages: list[dict[str, Any]]) -> str:
    """One model turn over the thread; the text, trimmed to the SMS cap."""
    org_cfgs = await resolve_org_configs(claimed.agent.organization_id)
    llm = build_llm(None, org_cfgs.get("llm"))
    parts: list[str] = []
    async with llm.chat(chat_ctx=_chat_context(messages)) as stream:
        async for chunk in stream:
            delta = getattr(chunk, "delta", None)
            if delta is not None and delta.content:
                parts.append(delta.content)
    text = "".join(parts).strip()
    return text[:MAX_REPLY_CHARS]


async def reply_once(api: AgentApiClient) -> bool:
    """Claim one pending text and answer it. Returns False when none waited."""
    async with session_scope() as db:
        claimed = await claim_pending_reply(db)
        if claimed is None:
            return False
        sms = claimed.sms
        try:
            history = await thread_messages(db, sms)
            messages = build_chat_messages(claimed.agent, history)
            text = await asyncio.wait_for(
                generate_reply(claimed, messages), timeout=LLM_TIMEOUT_SECONDS
            )
        except ProviderKeyError as exc:
            logger.warning("sms_id=%s text agent provider error: %s", sms.id, exc)
            await finish_reply(db, sms, "failed")
            return True
        except Exception:
            logger.exception("sms_id=%s text agent failed to write a reply", sms.id)
            await finish_reply(db, sms, "failed")
            return True
        if not text:
            logger.info("sms_id=%s text agent wrote nothing; skipped", sms.id)
            await finish_reply(db, sms, "skipped")
            return True
        try:
            result = await api.post(
                "/internal/agent/reply-sms", {"sms_id": str(sms.id), "body": text}
            )
            state = str(result.get("state") or "failed")
            if state not in ("done", "skipped", "failed"):
                state = "failed"
            if state != "done":
                logger.info(
                    "sms_id=%s reply %s: %s", sms.id, state, result.get("reason")
                )
        except Exception:
            logger.exception("sms_id=%s reply-sms POST failed", sms.id)
            state = "failed"
        await finish_reply(db, sms, state)  # type: ignore[arg-type]
        return True


async def run_forever(stop: threading.Event | None = None) -> None:
    api = AgentApiClient(settings.hail_api_url, settings.hail_internal_secret)
    logger.info("text worker started (poll every %ss)", POLL_SECONDS)
    try:
        while stop is None or not stop.is_set():
            try:
                busy = await reply_once(api)
            except Exception:
                logger.exception("text worker iteration failed; will retry")
                busy = False
            if not busy:
                await asyncio.sleep(POLL_SECONDS)
    finally:
        await api.aclose()


def start_thread() -> threading.Thread | None:
    """Start the worker in a daemon thread; None when the API secret is unset
    (self-host without the internal routes: texts reach webhooks only)."""
    if not settings.hail_internal_secret or not settings.hail_api_url:
        logger.warning(
            "text worker disabled: HAIL_INTERNAL_SECRET or HAIL_API_URL unset"
        )
        return None

    def _run() -> None:
        asyncio.run(run_forever())

    thread = threading.Thread(target=_run, name="hail-textbot", daemon=True)
    thread.start()
    return thread
