"""Email worker: the agent answers inbound email.

Polled by the text worker's loop in ``textbot``, with the same LLM precedence
and keys. The DB side lives in ``hailhq.core.email_agent``. The send goes through
``POST /internal/agent/reply-email``, where the scope, thread cap, funds,
suppression, billing, and delivery live.

Tool sends run during the claim, before the reply. Their routes refuse a second
send to the same person for one inbound email, so a retried run cannot repeat
one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import logfire
from hailhq.core.agent_tools.client import AgentApiClient
from hailhq.core.agent_tools.email_run import email_run_tools
from hailhq.core.agent_tools.spec import ToolContext, ToolSpec
from hailhq.core.db import session_scope
from hailhq.core.email_agent import (
    MAX_REPLY_CHARS,
    ClaimedEmailReply,
    ReplyState,
    build_chat_messages,
    claim_pending_reply,
    email_thread,
    finish_reply,
    retry_reply,
    sender_refusal,
)
from hailhq.core.forward_targets import normalize_address
from hailhq.core.telemetry import operation, telemetry_enabled
from hailhq.core.telemetry_identity import (
    identity_scope,
    resolve_identity,
    tag_context,
)
from hailhq.voicebot.chat_loop import MAX_TOOL_ROUNDS, run_chat
from hailhq.voicebot.pipeline import ProviderKeyError, build_llm, resolve_org_configs
from hailhq.voicebot.tools import wrap_tool
from livekit.agents.llm import FunctionToolCall

logger = logging.getLogger("hailhq.voicebot.emailbot")

# Per model call. One reply may make MAX_TOOL_ROUNDS of them.
LLM_TIMEOUT_SECONDS = 30.0
TOOL_FAILED = "That did not work."

__all__ = ["TOOL_FAILED", "generate_reply", "reply_once"]


def _tool_context(claimed: ClaimedEmailReply, api: AgentApiClient) -> ToolContext:
    """The run's tools are bound to the row, never to the model's arguments."""
    return ToolContext(
        call_id=None,
        organization_id=claimed.agent.organization_id,
        api=api,
        hangup=None,
        send_dtmf=None,
        inbound_email_id=claimed.email.id,
    )


async def _run_tool(
    call: FunctionToolCall, specs: dict[str, ToolSpec], tctx: ToolContext
) -> tuple[str, bool]:
    """``(output, is_error)`` for one tool call. Never raises."""
    spec = specs.get(call.name)
    if spec is None:
        return TOOL_FAILED, True
    try:
        args = json.loads(call.arguments or "{}")
    except ValueError:
        args = {}
    if not isinstance(args, dict):
        args = {}
    try:
        return str(await spec.execute(tctx, args) or ""), False
    except Exception:
        logger.exception("email agent tool %s failed", call.name)
        return TOOL_FAILED, True


async def generate_reply(
    claimed: ClaimedEmailReply,
    messages: list[dict[str, Any]],
    specs: list[ToolSpec],
    api: AgentApiClient,
) -> str:
    """The model's reply over the chat, trimmed to the reply cap."""
    org_cfgs = await resolve_org_configs(claimed.agent.organization_id)
    llm = build_llm(None, org_cfgs.get("llm"))
    tctx = _tool_context(claimed, api)
    by_name = {s.name: s for s in specs}

    async def run_tool(call: FunctionToolCall) -> tuple[str, bool]:
        return await _run_tool(call, by_name, tctx)

    text = await run_chat(
        llm,
        messages,
        [wrap_tool(s, tctx) for s in specs],
        run_tool,
        max_rounds=MAX_TOOL_ROUNDS,
        log_label=f"email_id={claimed.email.id}",
    )
    return text[:MAX_REPLY_CHARS]


async def _prepare(
    claimed: ClaimedEmailReply,
) -> tuple[list[dict[str, Any]], list[ToolSpec]]:
    """Load the thread and tools on a short session; nothing stays open."""
    email = claimed.email
    async with session_scope() as db:
        thread = await email_thread(db, email)
        specs = await email_run_tools(claimed.agent, db)
        refusal = await sender_refusal(
            db,
            claimed.domain,
            sender=normalize_address(email.from_address),
            dmarc_verdict=email.dmarc_verdict,
        )
    # Reaching here under scope anyone says nothing about who the sender is.
    member = claimed.domain.email_agent_scope == "members" and refusal is None
    return build_chat_messages(claimed.agent, thread, member=member), specs


async def _settle(
    claimed: ClaimedEmailReply, state: ReplyState | None, *, retry: bool = False
) -> None:
    async with session_scope() as db:
        if retry:
            await retry_reply(db, claimed)
        elif state is not None:
            await finish_reply(db, claimed.email, state, attempt=claimed.attempt)


async def reply_once(api: AgentApiClient) -> bool:
    """Claim one pending email and answer it. False when none waited.

    The claim commits first, so no row lock or connection is held while the
    model writes. A transient error puts the email back for a later retry; the
    reply route is idempotent per ``email_id``.
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
            logger.warning("email_id=%s actor lookup failed", claimed.email.id)
    with identity_scope(identity), operation(
        "invoke_agent hail-emailbot",
        **{
            "gen_ai.operation.name": "invoke_agent",
            "gen_ai.agent.name": "hail-emailbot",
            "email_id": str(claimed.email.id),
            "agent_id": str(claimed.agent.id),
            "organization_id": str(claimed.agent.organization_id),
            "attempt": claimed.attempt,
        },
    ) as span:
        tag_context(
            channel="email",
            direction="inbound",
            agent_id=claimed.agent.id,
            email_id=claimed.email.id,
            from_=claimed.email.from_address,
        )
        return await _reply_claimed(api, claimed, span)


async def _reply_claimed(
    api: AgentApiClient, claimed: ClaimedEmailReply, span=None
) -> bool:
    email = claimed.email
    try:
        messages, specs = await _prepare(claimed)
        text = await asyncio.wait_for(
            generate_reply(claimed, messages, specs, api),
            timeout=LLM_TIMEOUT_SECONDS * MAX_TOOL_ROUNDS,
        )
    except ProviderKeyError as exc:
        if span is not None:
            span.set_attribute("reply.state", "failed")
            span.record_exception(exc)
        # A missing or bad key does not fix itself: no retry.
        logger.warning("email_id=%s provider error: %s", email.id, exc)
        await _settle(claimed, "failed")
        return True
    except Exception as exc:
        if span is not None:
            span.set_attribute("reply.state", "retry")
            span.record_exception(exc)
        logger.exception(
            "email_id=%s failed to write a reply (attempt %s)",
            email.id,
            claimed.attempt,
        )
        await _settle(claimed, None, retry=True)
        return True
    if not text:
        if span is not None:
            span.set_attribute("reply.state", "skipped")
        logger.info("email_id=%s wrote nothing; skipped", email.id)
        await _settle(claimed, "skipped")
        return True
    try:
        result = await api.post(
            "/internal/agent/reply-email", {"email_id": str(email.id), "body": text}
        )
    except Exception as exc:
        if span is not None:
            span.set_attribute("reply.state", "retry")
            span.record_exception(exc)
        logger.exception(
            "email_id=%s reply-email POST failed (attempt %s)",
            email.id,
            claimed.attempt,
        )
        await _settle(claimed, None, retry=True)
        return True
    state = str(result.get("state") or "failed")
    if state not in ("done", "skipped", "failed"):
        state = "failed"
    if state != "done":
        logger.info("email_id=%s reply %s: %s", email.id, state, result.get("reason"))
    if span is not None:
        span.set_attribute("reply.state", state)
    await _settle(claimed, state)  # type: ignore[arg-type]
    return True
