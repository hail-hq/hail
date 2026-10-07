"""Text worker: the agent answers inbound SMS.

Runs inside the voicebot process (``main.py`` starts it in a daemon thread
with its own event loop) so text replies use the same LLM precedence and
keys as calls: ``build_llm(None, org_llm)`` (org BYO with optional fallback,
else the house chain). The DB side lives in ``hailhq.core.text_agent``; the
send goes through ``POST /internal/agent/reply-sms`` where the thread cap,
funds, suppression, billing and delivery live.

Process model: this thread is the only DB user in the worker's main process
(call jobs run in child processes that LiveKit starts with ``spawn`` or a
``forkserver``, never a fork of this process), so the shared engine in
``hailhq.core.db`` is created on this thread's loop and used only here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any

import logfire
from hailhq.core.agent_tools.client import AgentApiClient
from hailhq.core.agent_tools.spec import ToolContext
from hailhq.core.agent_tools.thread_history import SPEC as THREAD_HISTORY
from hailhq.core.config import settings
from hailhq.core.db import session_scope
from hailhq.core.telemetry import operation, telemetry_enabled
from hailhq.core.telemetry_identity import identity_scope, resolve_identity
from hailhq.core.text_agent import (
    MAX_REPLY_CHARS,
    ClaimedReply,
    ReplyState,
    build_chat_messages,
    claim_pending_reply,
    finish_reply,
    retry_reply,
    thread_history_for_reply,
)
from hailhq.core.threads import ThreadScope
from hailhq.voicebot.pipeline import ProviderKeyError, build_llm, resolve_org_configs
from hailhq.voicebot.tools import wrap_tool
from livekit.agents.llm import (
    ChatContext,
    FunctionCall,
    FunctionCallOutput,
    FunctionToolCall,
)

logger = logging.getLogger("hailhq.voicebot.textbot")

POLL_SECONDS = 2.0
LLM_TIMEOUT_SECONDS = 30.0
# Model calls per reply. The last one may not call a tool, so it writes text.
MAX_TOOL_ROUNDS = 3
# What the model reads when a tool call fails or names a tool it does not have.
TOOL_APOLOGY = "Sorry, the earlier messages could not be read."

__all__ = [
    "MAX_TOOL_ROUNDS",
    "POLL_SECONDS",
    "TOOL_APOLOGY",
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


def _tool_context(claimed: ClaimedReply) -> ToolContext:
    """The text agent's only tool reads this caller's thread, fixed by the
    inbound text, never by the model's arguments."""
    org = claimed.agent.organization_id
    sms = claimed.sms
    return ToolContext(
        call_id=None,
        organization_id=org,
        api=None,
        hangup=None,
        send_dtmf=None,
        thread=ThreadScope(org, claimed.agent.id, sms.from_e164, sms.to_e164),
    )


async def _run_tool(call: FunctionToolCall, tctx: ToolContext) -> tuple[str, bool]:
    """``(output, is_error)`` for one tool call. Never raises."""
    if call.name != THREAD_HISTORY.name:
        return TOOL_APOLOGY, True
    try:
        args = json.loads(call.arguments or "{}")
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        return await THREAD_HISTORY.execute(tctx, args), False
    except Exception:
        logger.exception("text agent tool %s failed", call.name)
        return TOOL_APOLOGY, True


async def generate_reply(claimed: ClaimedReply, messages: list[dict[str, Any]]) -> str:
    """The model's reply over the chat, trimmed to the SMS cap. The model may
    call ``thread_history``; each call's result goes back into the chat and
    the model is asked again, for at most ``MAX_TOOL_ROUNDS`` model calls."""
    org_cfgs = await resolve_org_configs(claimed.agent.organization_id)
    llm = build_llm(None, org_cfgs.get("llm"))
    tctx = _tool_context(claimed)
    tools = [wrap_tool(THREAD_HISTORY, tctx)]
    chat_ctx = _chat_context(messages)
    text = ""
    try:
        for round_no in range(1, MAX_TOOL_ROUNDS + 1):
            last = round_no == MAX_TOOL_ROUNDS
            parts: list[str] = []
            calls: list[FunctionToolCall] = []
            async with llm.chat(
                chat_ctx=chat_ctx, tools=tools, tool_choice="none" if last else "auto"
            ) as stream:
                async for chunk in stream:
                    delta = getattr(chunk, "delta", None)
                    if delta is None:
                        continue
                    if delta.content:
                        parts.append(delta.content)
                    calls.extend(delta.tool_calls or [])
            # Text beside a tool call ("let me check") is not the reply.
            text = "".join(parts).strip()
            if not calls or last:
                break
            for call in calls:
                output, is_error = await _run_tool(call, tctx)
                logger.info(
                    "sms_id=%s text agent tool=%s round=%d error=%s",
                    claimed.sms.id,
                    call.name,
                    round_no,
                    is_error,
                )
                chat_ctx.insert(
                    FunctionCall(
                        call_id=call.call_id,
                        name=call.name,
                        arguments=call.arguments,
                        extra=call.extra or {},
                    )
                )
                chat_ctx.insert(
                    FunctionCallOutput(
                        call_id=call.call_id,
                        name=call.name,
                        output=output,
                        is_error=is_error,
                    )
                )
    finally:
        # Plugin LLMs own an HTTP client each; one is built per reply.
        await llm.aclose()
    return text[:MAX_REPLY_CHARS]


async def _prepare(claimed: ClaimedReply) -> list[dict[str, Any]]:
    """Load the thread on a short session; nothing stays open afterwards."""
    async with session_scope() as db:
        history = await thread_history_for_reply(db, claimed.sms, claimed.agent)
    return build_chat_messages(claimed.agent, history)


async def _settle(
    claimed: ClaimedReply, state: ReplyState | None, *, retry: bool = False
) -> None:
    async with session_scope() as db:
        if retry:
            await retry_reply(db, claimed)
        elif state is not None:
            await finish_reply(db, claimed.sms, state, attempt=claimed.attempt)


async def reply_once(api: AgentApiClient) -> bool:
    """Claim one pending text and answer it. Returns False when none waited.

    The claim commits first, so no row lock or connection is held while the
    model writes and the API sends. A transient error (model call, API call)
    puts the text back for a later retry; the API's reply-sms route is
    idempotent per ``sms_id``, so a retried send cannot double-send.
    """
    with logfire.suppress_instrumentation():
        async with session_scope() as db:
            claimed = await claim_pending_reply(db)
    if claimed is None:
        return False
    identity = {}
    if telemetry_enabled():
        try:
            async with session_scope() as db:
                identity = await resolve_identity(db, claimed.agent.organization_id)
        except Exception:
            # Telemetry enrichment must not strand a claimed reply.
            logger.warning("sms_id=%s actor telemetry lookup failed", claimed.sms.id)
    with identity_scope(identity), operation(
        "invoke_agent hail-textbot",
        **{
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "hail-textbot",
            "sms_id": str(claimed.sms.id),
            "agent_id": str(claimed.agent.id),
            "organization_id": str(claimed.agent.organization_id),
            "attempt": claimed.attempt,
        },
    ) as span:
        return await _reply_claimed(api, claimed, span)


async def _reply_claimed(api: AgentApiClient, claimed: ClaimedReply, span=None) -> bool:
    sms = claimed.sms
    try:
        messages = await _prepare(claimed)
        text = await asyncio.wait_for(
            generate_reply(claimed, messages), timeout=LLM_TIMEOUT_SECONDS
        )
    except ProviderKeyError as exc:
        if span is not None:
            span.set_attribute("reply.state", "failed")
            span.record_exception(exc)
        # A missing or bad key does not fix itself: no retry.
        logger.warning("sms_id=%s text agent provider error: %s", sms.id, exc)
        await _settle(claimed, "failed")
        return True
    except Exception as exc:
        if span is not None:
            span.set_attribute("reply.state", "retry")
            span.record_exception(exc)
        logger.exception(
            "sms_id=%s text agent failed to write a reply (attempt %s)",
            sms.id,
            claimed.attempt,
        )
        await _settle(claimed, None, retry=True)
        return True
    if not text:
        if span is not None:
            span.set_attribute("reply.state", "skipped")
        logger.info("sms_id=%s text agent wrote nothing; skipped", sms.id)
        await _settle(claimed, "skipped")
        return True
    try:
        result = await api.post(
            "/internal/agent/reply-sms", {"sms_id": str(sms.id), "body": text}
        )
    except Exception as exc:
        if span is not None:
            span.set_attribute("reply.state", "retry")
            span.record_exception(exc)
        logger.exception(
            "sms_id=%s reply-sms POST failed (attempt %s)", sms.id, claimed.attempt
        )
        await _settle(claimed, None, retry=True)
        return True
    state = str(result.get("state") or "failed")
    if state not in ("done", "skipped", "failed"):
        state = "failed"
    if state != "done":
        logger.info("sms_id=%s reply %s: %s", sms.id, state, result.get("reason"))
    if span is not None:
        span.set_attribute("reply.state", state)
    await _settle(claimed, state)  # type: ignore[arg-type]
    return True


async def _poll_loop(api: AgentApiClient, stop: threading.Event | None) -> None:
    while stop is None or not stop.is_set():
        try:
            busy = await reply_once(api)
        except Exception:
            logger.exception("text worker iteration failed; will retry")
            busy = False
        if not busy:
            await asyncio.sleep(POLL_SECONDS)


async def run_forever(stop: threading.Event | None = None) -> None:
    api = AgentApiClient(settings.hail_api_url, settings.hail_internal_secret)
    n = settings.hail_text_reply_concurrency
    logger.info("text worker started (%s at once, poll every %ss)", n, POLL_SECONDS)
    try:
        await asyncio.gather(*(_poll_loop(api, stop) for _ in range(n)))
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
